#!/usr/bin/env python3
"""
metno_forecast.py — Fetch MET Norway forecast, write to InfluxDB with
forecast-issued time and horizon tags for RAG error computation.

Stores each forecast timepoint at its valid_time with:
  - tag: horizon = {N}h  (0h, 1h, 3h, 6h, 12h, 24h)
  - field: forecast_issued = epoch seconds of when this forecast was issued

Query pattern:
  observed at time T     -> bucket=weatherdata, _measurement=mqtt_consumer
  forecast valid at T    -> bucket=weather_forecast, horizon="1h", _time=T
  error = observed - forecast_1h
"""

import sys
import os
import json
from pathlib import Path
from email.utils import parsedate_to_datetime
import logging
from datetime import datetime, timezone, timedelta

import requests
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client.rest import ApiException

# ── Config ────────────────────────────────────────────────────────────
LAT = float(os.getenv("WEATHER_LATITUDE", "0"))
LON = float(os.getenv("WEATHER_LONGITUDE", "0"))
ALTITUDE_M = float(os.getenv("WEATHER_ALTITUDE_M", "243"))
CACHE_PATH = Path(os.getenv("METNO_CACHE", str(Path(__file__).with_name("metno_cache.json"))))

INFLUX_URL = os.getenv("INFLUX_URL", "http://127.0.0.1:8086")
INFLUX_TOKEN  = os.environ["INFLUX_TOKEN"]
INFLUX_ORG = os.getenv("INFLUX_ORG", "weather")
INFLUX_BUCKET = "weather_forecast"

USER_AGENT = os.environ.get("METNO_USER_AGENT", "weatherdash/1.0 (configure-contact-email)")

# Target forecast horizons in hours — these become tags.
# MET Norway compact returns hourly data; we only keep horizons we care about.
TARGET_HORIZONS_H = set(range(13)) | {24}
MAX_FORECAST_HOURS = 48  # only store first 48h of forecast horizon

# ── Logging ───────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] metno_forecast: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)


def nearest_horizon(hours: float) -> int | None:
    """Round a forecast horizon to the nearest target hour bucket."""
    rounded = int(round(hours))
    if rounded in TARGET_HORIZONS_H:
        return rounded
    return None


def create_bucket_if_missing(client: InfluxDBClient):
    """Create the target bucket if it doesn't exist."""
    buckets_api = client.buckets_api()
    for b in buckets_api.find_buckets().buckets:
        if b.name == INFLUX_BUCKET:
            log.info(f"Bucket '{INFLUX_BUCKET}' already exists")
            return
    org_api = client.organizations_api()
    org = org_api.find_organizations(org=INFLUX_ORG)[0]
    buckets_api.create_bucket(bucket_name=INFLUX_BUCKET, org_id=org.id)
    log.info(f"Created bucket '{INFLUX_BUCKET}'")


def fetch_forecast() -> dict:
    """Fetch MET Norway compact forecast, return parsed JSON."""
    cached = {}
    try:
        cached = json.loads(CACHE_PATH.read_text())
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc)
    if cached.get("expires") and cached.get("data"):
        try:
            if parsedate_to_datetime(cached["expires"]) > now:
                return cached["data"]
        except (ValueError, TypeError):
            pass
    url = ("https://api.met.no/weatherapi/locationforecast/2.0/compact"
           f"?lat={LAT}&lon={LON}&altitude={ALTITUDE_M:g}")
    headers = {"User-Agent": USER_AGENT}
    if cached.get("last_modified") and cached.get("data"):
        headers["If-Modified-Since"] = cached["last_modified"]
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    if response.status_code == 304:
        if not cached.get("data"):
            raise ValueError("304 response without cached forecast")
        data = cached["data"]
    else:
        data = response.json()
    updated = {"data": data,
               "expires": response.headers.get("Expires"),
               "last_modified": response.headers.get("Last-Modified", cached.get("last_modified"))}
    temporary = CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(updated))
    temporary.replace(CACHE_PATH)
    return data


def build_points(data: dict, issued_time: datetime) -> list[Point]:
    """Parse MET Norway JSON into InfluxDB Points aligned at valid_time."""
    retrieved_time = issued_time
    provider_updated = data.get("properties", {}).get("meta", {}).get("updated_at")
    if provider_updated:
        issued_time = datetime.fromisoformat(provider_updated.replace("Z", "+00:00"))
    points: list[Point] = []
    units = data.get("properties", {}).get("meta", {}).get("units", {})
    log.info(f"MET Norway units: {units}")

    for ts_entry in data.get("properties", {}).get("timeseries", []):
        valid_time_str = ts_entry.get("time", "")
        if not valid_time_str:
            continue

        valid_time = datetime.fromisoformat(valid_time_str.replace("Z", "+00:00"))

        # Horizon in hours
        horizon_delta = valid_time - retrieved_time
        horizon_hours = horizon_delta.total_seconds() / 3600.0

        # Skip if beyond our range or not at a target horizon
        if horizon_hours < -1 or horizon_hours > MAX_FORECAST_HOURS:
            continue

        tag_horizon = nearest_horizon((valid_time - retrieved_time.replace(minute=0, second=0, microsecond=0)).total_seconds() / 3600)
        if tag_horizon is None:
            continue

        # ── Instant details ──────────────────────────────────────────
        instant = ts_entry.get("data", {}).get("instant", {}).get("details", {})
        if not instant:
            continue

        temp = instant.get("air_temperature")
        wind_speed = instant.get("wind_speed")
        wind_dir = instant.get("wind_from_direction")
        pressure = instant.get("air_pressure_at_sea_level")
        humidity = instant.get("relative_humidity")

        # ── Precipitation from next_1_hours / next_6_hours ──────────
        precip = None
        precip_period = None
        symbol = None
        for key in ("next_1_hours", "next_6_hours"):
            block = ts_entry.get("data", {}).get(key, {})
            details = block.get("details", {})
            if "precipitation_amount" in details:
                precip = details["precipitation_amount"]
                precip_period = 1 if key == "next_1_hours" else 6
                symbol = block.get("summary", {}).get("symbol_code")
                break

        point = (
            Point("metno_forecast")
            .tag("source", "metno")
            .tag("run_id", issued_time.isoformat())
            .tag("horizon", f"{tag_horizon}h")
            .time(valid_time)
        )

        if temp is not None:
            point = point.field("temp", float(temp))
        if wind_speed is not None:
            point = point.field("wind_speed", float(wind_speed))
        if wind_dir is not None:
            point = point.field("wind_dir", float(wind_dir))
        if pressure is not None:
            point = point.field("pressure", float(pressure))
        if humidity is not None:
            point = point.field("humidity", float(humidity))
        if precip is not None:
            point = point.field("precipitation", float(precip))
            point = point.field("precipitation_period_hours", int(precip_period))
        if symbol:
            point = point.field("symbol_code", symbol)

        point = point.field("retrieved_at", float(retrieved_time.timestamp()))

        # Preserve provider issue time separately from retrieval time.
        point = point.field("forecast_issued", int(issued_time.timestamp()))
        point = point.field("horizon_seconds", int(horizon_delta.total_seconds()))

        points.append(point)

    return points


def write_points(client: InfluxDBClient, points: list[Point]):
    """Write forecast points to InfluxDB."""
    if not points:
        log.warning("No forecast points to write")
        return

    write_api = client.write_api(write_options=SYNCHRONOUS)
    try:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
        log.info(f"Wrote {len(points)} forecast points to '{INFLUX_BUCKET}'")
    except ApiException as e:
        log.error(f"InfluxDB write API error: {e}")
        raise
    except Exception as e:
        log.error(f"InfluxDB write failed: {e}")
        raise


def main():
    log.info("=== MET Norway forecast ingester starting ===")

    now = datetime.now(timezone.utc)
    issued_time = now

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)

    try:
        create_bucket_if_missing(client)
        data = fetch_forecast()
        points = build_points(data, issued_time)
        write_points(client, points)
    except requests.exceptions.RequestException as e:
        log.error(f"HTTP request failed: {e}")
        sys.exit(1)
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        sys.exit(1)
    finally:
        client.close()

    log.info("=== MET Norway forecast ingester finished ===")


if __name__ == "__main__":
    main()
