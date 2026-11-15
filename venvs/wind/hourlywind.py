#!/usr/bin/env python3
"""
hourlywind.py — Hourly prevailing wind direction service
Reads wind direction readings from InfluxDB each hour,
votes into 8 compass segments, writes result back to InfluxDB.

Each hour writes a single point with field "present"=1 tagged with
the prevailing compass segment. Grafana queries each segment separately
giving equal-height colour-coded bars.
"""

from influxdb_client import os
import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime, timezone, timedelta
import threading
import logging
import sys

INFLUX_URL = os.getenv("INFLUX_URL", "http://127.0.0.1:8086")
INFLUX_TOKEN  = os.environ["INFLUX_TOKEN"]
INFLUX_ORG = os.getenv("INFLUX_ORG", "weather")
INFLUX_BUCKET = "weatherdata"

SEGMENTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
SEGMENT_VAL = {s: i for i, s in enumerate(SEGMENTS)}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

influx    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx.write_api(write_options=SYNCHRONOUS)
query_api = influx.query_api()

def degrees_to_segment(degrees: float) -> str:
    degrees = degrees % 360
    idx = int((degrees + 22.5) / 45) % 8
    return SEGMENTS[idx]

def compute_prevailing(hour_start: datetime, hour_end: datetime) -> dict | None:
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {hour_start.strftime("%Y-%m-%dT%H:%M:%SZ")},
           stop:  {hour_end.strftime("%Y-%m-%dT%H:%M:%SZ")})
  |> filter(fn: (r) => r._measurement == "mqtt_consumer" and r._field == "direction")
  |> yield(name: "directions")
'''
    try:
        tables = query_api.query(flux, org=INFLUX_ORG)
    except Exception as e:
        log.error(f"InfluxDB query failed: {e}")
        return None

    readings = []
    for table in tables:
        for record in table.records:
            val = record.get_value()
            if val is not None:
                readings.append(float(val))

    if not readings:
        log.warning(f"No direction data for {hour_start.strftime('%H:%M')}–{hour_end.strftime('%H:%M')}")
        return None

    votes: dict[str, int] = {s: 0 for s in SEGMENTS}
    for deg in readings:
        votes[degrees_to_segment(deg)] += 1

    prevailing = max(votes, key=lambda s: votes[s])
    total = len(readings)

    log.info(
        f"Hour {hour_start.strftime('%H:%M')}: {total} readings, "
        f"prevailing={prevailing} ({votes[prevailing]}/{total} votes) | "
        f"votes={votes}"
    )

    return {
        "prevailing": prevailing,
        "votes":      votes[prevailing],
        "total":      total,
        "pct":        round(votes[prevailing] / total * 100, 1)
    }

def write_prevailing(hour_start: datetime, result: dict):
    # Write present=1 tagged with the compass segment
    # Grafana queries each segment as a separate series — equal height, distinct colour
    point = (
        Point("wind_prevailing")
        .tag("station",   "esp32-c3-weather")
        .tag("location",  "575 Moonshine Hill Road")
        .tag("direction", result["prevailing"])
        .field("segment_index", SEGMENT_VAL[result["prevailing"]])
        .field("votes",   result["votes"])
        .field("total",   result["total"])
        .field("pct",     result["pct"])
        .time(hour_start, write_precision="s")
    )
    try:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
        log.info(f"Written: {result['prevailing']} at {hour_start.strftime('%Y-%m-%dT%H:%MZ')}")
    except Exception as e:
        log.error(f"InfluxDB write failed: {e}")

class HourlyScheduler:
    def __init__(self):
        self._schedule_next()

    def _schedule_next(self):
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(
            minute=0, second=5, microsecond=0)
        delay = (next_hour - now).total_seconds()
        log.info(f"Next run in {delay:.0f}s at {next_hour.strftime('%H:%M:%S')} UTC")
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        now = datetime.now(timezone.utc)
        hour_end   = now.replace(minute=0, second=0, microsecond=0)
        hour_start = hour_end - timedelta(hours=1)
        result = compute_prevailing(hour_start, hour_end)
        if result:
            write_prevailing(hour_start, result)
        self._schedule_next()

    def stop(self):
        self._timer.cancel()

def main():
    log.info("Hourly wind direction service starting")

    # Backfill last completed hour on startup
    now = datetime.now(timezone.utc)
    hour_end   = now.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_end - timedelta(hours=1)
    log.info(f"Backfilling: {hour_start.strftime('%H:%M')}–{hour_end.strftime('%H:%M')}")
    result = compute_prevailing(hour_start, hour_end)
    if result:
        write_prevailing(hour_start, result)

    scheduler = HourlyScheduler()

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        scheduler.stop()
        influx.close()

if __name__ == "__main__":
    main()
