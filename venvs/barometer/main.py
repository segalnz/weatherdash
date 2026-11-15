import os
import math
from datetime import datetime, timezone, timedelta
import zoneinfo

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from forecast_state import (
    ForecastStrip,
    ForecastSlot,
    PressureTendency,
    AtmosphericState,
)
from influx_reader import (
    get_latest_rooftop,
    get_recent_hours_data,
    get_recent_pressure,
    get_rainfall_series,
    get_raw_metno_fields,
)
from derived_features import compute_derived_features
from sensor_quality import assess_humidity
from heuristics import compute_now_forecast, _project_horizon, compute_pressure_tendency
from local_forecast import interpret, adjust_temperature
from svg_renderer import render_forecast_strip
from synthesis import synthesise

app = FastAPI(title="Moonshine Valley Barometer")

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>Moonshine Valley Barometer</h1><p>Static index.html not found.</p>")


@app.get("/api/forecast", response_model=ForecastStrip)
def api_forecast():
    return _display_forecast()


@app.get("/api/verification")
def api_verification():
    """Read the scheduled report; never run retrospective queries on display."""
    import json
    from pathlib import Path
    path = Path(os.getenv("FORECAST_VERIFICATION_REPORT", "/var/lib/weatherdash/verification_report.json"))
    try:
        report = json.loads(path.read_text())
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(report['generated_at'])).total_seconds()
        if report.get('schema_version') != 2 or not isinstance(report.get('cohorts'), list):
            raise ValueError('Unsupported report')
        if not -30 <= age < 3600:
            return {'status': 'stale', 'message': 'Verification report awaiting a scheduled refresh.'}
        return dict(report, status='ok')
    except (OSError, ValueError, TypeError, KeyError):
        return {'status': 'unavailable', 'message': 'Verification results are not available yet.'}


@app.get("/api/forecast/svg")
def api_forecast_svg():
    strip = _display_forecast()
    svg = render_forecast_strip(strip)
    return Response(content=svg, media_type="image/svg+xml")


def _display_forecast():
    # The timer does sensor/API work; display requests read its atomic cache.
    import logging
    from pathlib import Path
    cache = Path(os.getenv("FORECAST_CACHE", "/var/lib/weatherdash/forecast_cache.json"))
    now = datetime.now(timezone.utc)
    try:
        strip = ForecastStrip.model_validate_json(cache.read_text())
        age = (now - datetime.fromisoformat(strip.generated_at_iso)).total_seconds()
        if 0 <= age <= 20 * 60:
            _expire_pressure(strip, now)
            if strip.synthesis and now >= datetime.fromisoformat(strip.synthesis.valid_until):
                strip.synthesis = None
            return strip
    except (OSError, ValueError, TypeError):
        pass
    logging.getLogger(__name__).warning("Forecast cache missing or stale")
    return ForecastStrip(generated_at="Forecast update unavailable", generated_at_iso=now.isoformat(),
        degraded=True, evidence=["Scheduled forecast unavailable; stale data withheld"],
        slots=[ForecastSlot(hour_offset=h, time_label=(now + timedelta(hours=h)).astimezone(zoneinfo.ZoneInfo("Pacific/Auckland")).strftime("%H:%M"),
               state=AtmosphericState.UNKNOWN, state_label="Forecast unavailable", icon="?",
               confidence=0, degraded=True, source="unavailable") for h in range(7)])


def _expire_pressure(strip, now):
    # The forecast cache can live longer than an observation's ten-minute limit.
    if strip.rooftop is None or strip.derived is None:
        return
    stamp = strip.rooftop.observation_times.get("pressure")
    try:
        age = (now - datetime.fromisoformat(stamp)).total_seconds()
        fresh = -30 <= age < 600
    except (TypeError, ValueError):
        fresh = False
    if not fresh:
        strip.rooftop.pressure_hpa = None
        strip.derived.pressure_delta_1h = None
        strip.derived.pressure_delta_3h = None
        strip.pressure_tendency = PressureTendency.UNKNOWN
        for slot in strip.slots:
            slot.pressure_tendency = PressureTendency.UNKNOWN


def _build_forecast(previous_synthesis=None) -> ForecastStrip:
    now = datetime.now(timezone.utc)

    try:
        nz_tz = zoneinfo.ZoneInfo("Pacific/Auckland")
    except Exception:
        nz_tz = timezone(timedelta(hours=13))
    nz_now = now.astimezone(nz_tz)
    generated = nz_now.strftime("%d/%m %H:%M NZ")

    rooftop = get_latest_rooftop()
    rooftop_df = get_recent_hours_data(hours=4)
    rain_df = get_rainfall_series(hours=4)
    derived = compute_derived_features(rooftop_df, rain_df, now=now)
    pressure_tendency = compute_pressure_tendency(derived)

    assess_humidity(rooftop, rooftop_df, now)
    now_fc = compute_now_forecast(rooftop, derived)
    evidence = interpret(rooftop, derived)
    if derived.pressure_delta_3h is None:
        evidence[0] = "Pressure trend withheld: gaps, stale readings or excessive variability"

    # Historical corrected_forecast points are verification data, not future
    # predictions. Use fresh, coherent MET data until future bias correction exists.
    raw_metno = get_raw_metno_fields(now)

    # Interpolate the model's instantaneous temperature to observation time.
    current_model = dict(raw_metno.get(0, {}))
    if current_model.get("temp") is not None and raw_metno.get(1, {}).get("temp") is not None:
        fraction = (now.minute * 60 + now.second) / 3600
        current_model["temp"] += fraction * (raw_metno[1]["temp"] - current_model["temp"])

    press_df = get_recent_pressure(hours=6)
    pressure_history: list[float | None] = []
    pressure_times: list[str] = []
    pressure_timestamps: list[str] = []
    if not press_df.empty:
        for t, pressure in press_df[["_time", "pressure"]].itertuples(index=False, name=None):
            if hasattr(t, "to_pydatetime"):
                t = t.to_pydatetime()
            if t.tzinfo is None:
                from datetime import timezone as dt_tz
                t = t.replace(tzinfo=dt_tz.utc)
            nz_t = t.astimezone(nz_tz)
            pressure_history.append(float(pressure) if math.isfinite(float(pressure)) else None)
            pressure_times.append(nz_t.strftime("%H:%M"))
            pressure_timestamps.append(t.isoformat())

    slots: list[ForecastSlot] = []

    for horizon_h in [0, 1, 2, 3, 4, 5, 6]:
        raw = raw_metno.get(horizon_h, {})

        if horizon_h == 0:
            temp_c = rooftop.temperature_c
        else:
            temp_c = raw.get("temp")

        adjustment = 0.0
        slot_evidence = list(evidence)
        if horizon_h > 0:
            lead = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=horizon_h) - now).total_seconds() / 3600
            temp_c, adjustment, reason = adjust_temperature(rooftop, derived, current_model, raw, lead)
            slot_evidence.append(reason)

        raw_wd = raw.get("wind_dir")
        raw_ws = raw.get("wind_speed")

        metno_ws_kmh = raw_ws * 3.6 if raw_ws is not None else None
        wd = rooftop.wind_direction_deg if horizon_h == 0 else raw_wd
        ws = rooftop.wind_speed_kmh if horizon_h == 0 else metno_ws_kmh

        if horizon_h == 0:
            state = now_fc.state
            label = now_fc.label
            confidence = now_fc.confidence
        else:
            proj_state, proj_label, _ = _project_horizon(now_fc, derived, horizon_h, raw)
            state = proj_state
            label = proj_label
            confidence = 0 if proj_label == "Forecast unavailable" else 50
            if confidence and (derived.pressure_delta_3h is None or rooftop.temperature_c is None):
                confidence = 35

        slot_time = now if horizon_h == 0 else now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=horizon_h)
        slot_nz = slot_time.astimezone(nz_tz)
        time_label = slot_nz.strftime("%H:%M")

        slot = ForecastSlot(
            hour_offset=horizon_h,
            evidence=slot_evidence,
            baseline_temperature_c=raw.get("temp"),
            baseline_wind_speed_kmh=metno_ws_kmh,
            baseline_wind_dir_deg=raw_wd,
            baseline_precipitation_mm=raw.get("precipitation") if raw.get("precipitation_period_hours") == 1 else None,
            forecast_pressure_hpa=raw.get("pressure"),
            forecast_humidity_pct=raw.get("humidity"),
            temperature_adjustment_c=adjustment,
            time_label=time_label,
            is_night=str(raw.get("symbol_code", "")).endswith("_night"),
            state=state,
            state_label=label,
            icon="",
            temperature_c=temp_c,
            wind_dir_deg=wd,
            wind_speed_kmh=ws,
            confidence=confidence,
            pressure_tendency=pressure_tendency,
            source="local-instruments" if horizon_h == 0 else (("metno+local-rules" if adjustment else "metno") if confidence else "unavailable"),
            degraded=(rooftop.rain_quality != "ok" or rooftop.humidity_quality != "ok") if horizon_h == 0 else confidence < 50 or temp_c is None,
            precipitation_mm=raw.get("precipitation") if raw.get("precipitation_period_hours") == 1 else None,
            forecast_valid_time=raw.get("valid_time"),
            forecast_issued=raw.get("forecast_issued"),
        )
        slots.append(slot)

    return ForecastStrip(
        synthesis=synthesise(rooftop, derived, raw_metno, now, previous=previous_synthesis),
        generated_at=generated,
        generated_at_iso=now.isoformat(),
        evidence=evidence,
        degraded=any(s.degraded for s in slots),
        slots=slots,
        rooftop=rooftop,
        derived=derived,
        pressure_tendency=pressure_tendency,
        pressure_history=pressure_history,
        pressure_history_times=pressure_times,
        pressure_history_timestamps=pressure_timestamps,
    )


def main():
    import uvicorn
    port = int(os.getenv("BAROMETER_PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port)
