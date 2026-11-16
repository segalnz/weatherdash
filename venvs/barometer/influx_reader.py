import os
import math
from datetime import datetime, timezone, timedelta

from influxdb_client import InfluxDBClient
import pandas as pd

from forecast_state import RooftopSnapshot, CorrectedForecast

INFLUX_URL = os.getenv("INFLUX_URL", "http://127.0.0.1:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "weather")

HORIZON_HOURS = [0, 1, 2, 3, 4, 5, 6]


def sensor_topic(field):
    if field == "pressure":
        return "pressure/telemetry"
    return "sht45/telemetry" if field in ("temperature", "humidity", "dewpoint", "quality_valid") else "status/weather/data"


def _get_client() -> InfluxDBClient:
    return InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)


def query_flux(flux_query: str) -> list:
    client = _get_client()
    try:
        tables = client.query_api().query(flux_query, org=INFLUX_ORG)
        records = []
        for table in tables:
            for record in table.records:
                records.append(record)
        return records
    finally:
        client.close()


def get_latest_rooftop() -> RooftopSnapshot:
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(minutes=10)

    fields_of_interest = ["temperature", "humidity", "pressure", "speed", "direction", "gust"]
    result = {}
    times = {}

    for field in fields_of_interest:
        if field == "pressure":
            rows = pressure_rows(start, stop)
            if rows:
                latest = max(rows, key=lambda r: r["_time"])
                value = valid_pressure(latest)
                if value is not None:
                    result[field] = value
                    times[field] = latest["_time"].isoformat()
            continue
        flux = (
            f'from(bucket: "weatherdata") '
            f'|> range(start: {start.isoformat()}, stop: {stop.isoformat()}) '
            f'|> filter(fn: (r) => r._measurement == "mqtt_consumer" and r._field == "{field}" and r.topic == "{sensor_topic(field)}") '
            f'|> last() '
        )
        records = query_flux(flux)
        if records:
            record = max(records, key=lambda r: r["_time"])
            value = float(record.get_value())
            if math.isfinite(value):
                result[field] = value
                times[field] = record["_time"].isoformat()

    snap = RooftopSnapshot()
    snap.observation_times = times
    snap.wind_gust_kmh = result["gust"] * 3.6 if "gust" in result else None
    snap.temperature_c = result.get("temperature")
    snap.humidity_pct = result.get("humidity")
    snap.pressure_hpa = result.get("pressure")
    speed_ms = result.get("speed")
    snap.wind_speed_kmh = speed_ms * 3.6 if speed_ms is not None else None
    snap.wind_direction_deg = result.get("direction")

    # A finite amount alone does not prove a complete rain-gauge interval.
    flux = (f'from(bucket: "weatherdata") |> range(start: {(stop - timedelta(minutes=25)).isoformat()}, stop: {stop.isoformat()}) '
            '|> filter(fn: (r) => r._measurement == "rainfall" and r.station == "davis-rainbucket") '
            '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")')
    records = query_flux(flux)
    if records:
        latest = max(records, key=lambda r: r["_time"])
        rain = latest.values.get("rainfall_mm")
        if (rain is not None and math.isfinite(rain) and rain >= 0
                and latest.values.get("slots_missing") == 0):
            snap.rain_recent_mm = rain
            snap.rain_detected = rain > 0
            snap.rain_quality = "ok"
            snap.rain_interval_start = latest["_time"].isoformat()
            snap.rain_interval_end = (latest["_time"] + timedelta(minutes=10)).isoformat()
            snap.observation_times["rain"] = snap.rain_interval_end

    # A newer invalid packet must not leave an older good reading looking current.
    quality = _get_latest_field("weatherdata", "mqtt_consumer", "quality_valid", stop, timedelta(minutes=10))
    if quality == 0:
        snap.temperature_c = None
        snap.humidity_pct = None

    snap.lux = _get_latest_field("weatherdata", "mqtt_consumer", "lux", stop, timedelta(minutes=10), station="esp32-c3-lux")
    snap.lux_condition = _get_latest_condition("weatherdata", "mqtt_consumer", "condition", stop, timedelta(minutes=10))

    return snap


def _get_latest_field(bucket: str, measurement: str, field: str, stop: datetime, lookback: timedelta, station: str | None = None) -> float | None:
    start = stop - lookback
    station_filter = f' and r.station == "{station}"' if station else ""
    topic_filter = f' and r.topic == "{sensor_topic(field)}"' if field != "lux" else ""
    flux = (
        f'from(bucket: "{bucket}") '
        f'|> range(start: {start.isoformat()}, stop: {stop.isoformat()}) '
        f'|> filter(fn: (r) => r._measurement == "{measurement}"{station_filter} and r._field == "{field}"{topic_filter}) '
        f'|> last() '
    )
    records = query_flux(flux)
    if records:
        v = max(records, key=lambda r: r["_time"]).get_value()
        if v is not None:
            return float(v) if math.isfinite(float(v)) else None
    return None


def _get_latest_condition(bucket: str, measurement: str, tag: str, stop: datetime, lookback: timedelta) -> str | None:
    start = stop - lookback
    flux = (
        f'from(bucket: "{bucket}") '
        f'|> range(start: {start.isoformat()}, stop: {stop.isoformat()}) '
        f'|> filter(fn: (r) => r._measurement == "{measurement}" and r.station == "esp32-c3-lux" and r._field == "lux") '
        f'|> last() '
    )
    records = query_flux(flux)
    if records:
        return str(records[0].values.get(tag, "")) or None
    return None


def get_recent_hours_data(hours: int = 4) -> pd.DataFrame:
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(hours=hours)

    fields_of_interest = ["temperature", "humidity", "pressure", "speed", "direction", "gust"]
    all_data = []

    for field in fields_of_interest:
        if field == "pressure":
            for stamp, value in pressure_means(start, stop):
                all_data.append({"_time": stamp, "field": field, "value": value})
            continue
        flux = (
            f'from(bucket: "weatherdata") '
            f'|> range(start: {start.isoformat()}) '
            f'|> filter(fn: (r) => r._measurement == "mqtt_consumer" and r._field == "{field}" and r.topic == "{sensor_topic(field)}") '
            f'|> aggregateWindow(every: 5m, fn: {"last" if field == "direction" else "max" if field == "gust" else "mean"}, createEmpty: false) '
        )
        records = query_flux(flux)
        for r in records:
            all_data.append({
                "_time": r["_time"],
                "field": field,
                "value": float(r.get_value()),
            })

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data)
    df["_time"] = pd.to_datetime(df["_time"])
    df = df.pivot_table(index="_time", columns="field", values="value", aggfunc="first")
    df = df.reset_index()
    df.columns.name = None
    df = df.sort_values("_time").reset_index(drop=True)
    return df


def get_recent_pressure(hours: int = 6, now: datetime | None = None) -> pd.DataFrame:
    stop = now or datetime.now(timezone.utc)
    start = stop - timedelta(hours=hours)

    data = [{"_time": stamp, "pressure": value} for stamp, value in pressure_means(start, stop)]
    return pd.DataFrame(data)


def pressure_means(start, stop):
    rows = pressure_rows(start, stop)
    if not rows:
        return []
    series = pd.Series([valid_pressure(r) for r in rows],
                       index=pd.to_datetime([r["_time"] for r in rows], utc=True), dtype=float)
    bins = series.resample("5min", label="right", closed="left")
    # Explicit invalid readings poison their bin; empty bins remain null.
    means = bins.agg(lambda values: values.mean() if len(values) and values.notna().all() else float("nan"))
    # A right-labelled partial bucket can end after the collection time.
    # It is not an observation from the future: publish completed buckets only.
    means = means.loc[means.index <= pd.Timestamp(stop)]
    return list(means.items())


def valid_pressure(row):
    values = row.values
    value = values.get("pressure")
    if (values.get("quality_valid") is not True or
            values.get("pressure_basis") != "legacy_sea_level_corrected" or
            value is None or not math.isfinite(float(value))):
        return None
    return float(value)


def pressure_rows(start, stop):
    # Pivot before choosing latest: a newer invalid event must mask old pressure.
    flux = (
        f'from(bucket: "weatherdata") '
        f'|> range(start: {start.isoformat()}, stop: {stop.isoformat()}) '
        '|> filter(fn: (r) => r._measurement == "mqtt_consumer" and r.topic == "pressure/telemetry" and r.station == "local-pressure") '
        '|> filter(fn: (r) => r._field == "pressure" or r._field == "quality_valid" or r._field == "pressure_basis") '
        '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
    )
    return query_flux(flux)


def get_rainfall_since(since: datetime) -> float:
    stop = datetime.now(timezone.utc)
    flux = (
        f'from(bucket: "weatherdata") '
        f'|> range(start: {since.isoformat()}, stop: {stop.isoformat()}) '
        f'|> filter(fn: (r) => r._measurement == "rainfall" and r._field == "rainfall_mm" and r.station == "davis-rainbucket") '
        f'|> sum() '
    )
    records = query_flux(flux)
    for r in records:
        v = r.get_value()
        if v is not None:
            return float(v)
    return 0.0


def get_rainfall_series(hours: int = 4) -> pd.DataFrame:
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(hours=hours)

    flux = (
        f'from(bucket: "weatherdata") '
        f'|> range(start: {start.isoformat()}) '
        f'|> filter(fn: (r) => r._measurement == "rainfall" and r._field == "rainfall_mm" and r.station == "davis-rainbucket") '
    )
    records = query_flux(flux)
    data = []
    for r in records:
        data.append({"_time": r["_time"], "rainfall_mm": float(r.get_value())})

    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["_time"] = pd.to_datetime(df["_time"])
    return df.sort_values("_time").reset_index(drop=True)


def get_corrected_forecasts() -> dict[int, CorrectedForecast]:
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(hours=6)

    results: dict[int, CorrectedForecast] = {}

    for horizon_h in HORIZON_HOURS:
        horizon_tag = f"{horizon_h}h" if horizon_h > 0 else "0h"
        fields = ["temp", "wind_speed", "wind_dir", "pressure", "humidity"]

        fc = CorrectedForecast()

        for field in fields:
            flux = (
                f'from(bucket: "forecast_derived") '
                f'|> range(start: {start.isoformat()}) '
                f'|> filter(fn: (r) => r._measurement == "corrected_forecast" '
                f'and r.horizon == "{horizon_tag}" and r._field == "{field}") '
                f'|> last() '
            )
            records = query_flux(flux)
            if records:
                val = float(records[0].get_value())
                if field == "temp":
                    fc.forecast_temp = val
                elif field == "wind_speed":
                    fc.forecast_wind_speed = val
                elif field == "wind_dir":
                    fc.forecast_wind_dir = val
                elif field == "pressure":
                    fc.forecast_pressure = val
                elif field == "humidity":
                    fc.forecast_humidity = val

        results[horizon_h] = fc

    return results


def get_raw_forecast_precip() -> dict[int, float]:
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(hours=6)

    results: dict[int, float] = {}

    for horizon_h in HORIZON_HOURS:
        horizon_tag = f"{horizon_h}h" if horizon_h > 0 else "0h"

        flux = (
            f'from(bucket: "weather_forecast") '
            f'|> range(start: {start.isoformat()}) '
            f'|> filter(fn: (r) => r._measurement == "metno_forecast" '
            f'and r.horizon == "{horizon_tag}" and r._field == "precipitation") '
            f'|> last() '
        )
        records = query_flux(flux)
        if records:
            results[horizon_h] = float(records[0].get_value())
        else:
            results[horizon_h] = 0.0

    return results


def select_forecast_rows(records: list, now: datetime) -> dict[int, dict]:
    """Select a coherent fresh run, indexing by valid time rather than old lead tags."""
    import math
    start = now.replace(minute=0, second=0, microsecond=0)
    candidates = []
    for record in records:
        row = dict(record.values)
        issued = row.get("forecast_issued")
        valid = row.get("_time")
        if issued is None or valid is None:
            continue
        issued = float(issued)
        retrieved = float(row.get("retrieved_at", issued))
        max_issue_age = 18 * 3600 if "retrieved_at" in row else 6 * 3600
        if (not math.isfinite(issued) or not 0 <= now.timestamp() - issued <= max_issue_age
                or not math.isfinite(retrieved) or not 0 <= now.timestamp() - retrieved <= (5400 if "retrieved_at" in row else 6 * 3600)):
            continue
        offset = (valid - start).total_seconds() / 3600
        if offset != int(offset) or not 0 <= offset <= 6:
            continue
        candidates.append((int(offset), issued, valid, row))
    if not candidates:
        return {}
    newest = max(item[1] for item in candidates)
    result = {}
    fields = ("temp", "wind_speed", "wind_dir", "pressure", "humidity",
              "precipitation", "precipitation_period_hours")
    for offset, issued, valid, row in sorted(candidates, key=lambda item: float(item[3].get("retrieved_at", item[1]))):
        if issued != newest:
            continue
        output = {"forecast_issued": issued, "valid_time": valid.isoformat(), "retrieved_at": row.get("retrieved_at")}
        for field in fields:
            value = row.get(field)
            if value is not None and math.isfinite(float(value)):
                output[field] = float(value)
        output["symbol_code"] = row.get("symbol_code")
        result[offset] = output
    return result


def get_raw_metno_fields(now: datetime | None = None) -> dict[int, dict]:
    import logging
    now = now or datetime.now(timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0)
    stop = start + timedelta(hours=7)
    flux = (
        f'from(bucket: "weather_forecast") '
        f'|> range(start: {start.isoformat()}, stop: {stop.isoformat()}) '
        '|> filter(fn: (r) => r._measurement == "metno_forecast" and r.source == "metno") '
        '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") '
    )
    try:
        return select_forecast_rows(query_flux(flux), now)
    except Exception:
        logging.getLogger(__name__).exception("MET forecast unavailable")
        return {}


def get_raw_forecast_wind() -> dict[int, float]:
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(hours=6)

    results: dict[int, float] = {}

    for horizon_h in HORIZON_HOURS:
        horizon_tag = f"{horizon_h}h" if horizon_h > 0 else "0h"

        flux = (
            f'from(bucket: "weather_forecast") '
            f'|> range(start: {start.isoformat()}) '
            f'|> filter(fn: (r) => r._measurement == "metno_forecast" '
            f'and r.horizon == "{horizon_tag}" and r._field == "wind_speed") '
            f'|> last() '
        )
        records = query_flux(flux)
        if records:
            results[horizon_h] = float(records[0].get_value())
        else:
            results[horizon_h] = 0.0

    return results


def get_last_n_forecast_runs(horizon_h: int, n: int = 3) -> pd.DataFrame:
    stop = datetime.now(timezone.utc)
    start = stop - timedelta(hours=48)
    horizon_tag = f"{horizon_h}h" if horizon_h > 0 else "0h"

    flux = (
        f'from(bucket: "weather_forecast") '
        f'|> range(start: {start.isoformat()}) '
        f'|> filter(fn: (r) => r._measurement == "metno_forecast" '
        f'and r.horizon == "{horizon_tag}" and r._field == "temp") '
    )
    records = query_flux(flux)
    data = []
    for r in records:
        fi = r.values.get("forecast_issued")
        if fi is not None:
            data.append({
                "_time": r["_time"],
                "temp": float(r.get_value()),
                "forecast_issued": float(fi),
            })

    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df = df.sort_values("_time")
    return df.tail(n)
