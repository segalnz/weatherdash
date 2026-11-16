"""Scheduled forecast creation and immutable issue-time archive for verification."""
import json
import logging
import os
from pathlib import Path
from datetime import datetime
import math
from synthesis import fresh
from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS
from main import _build_forecast
from forecast_state import ForecastStrip
from influx_reader import _get_client, INFLUX_ORG

CACHE = Path(os.getenv('FORECAST_CACHE', '/var/lib/weatherdash/forecast_cache.json'))


def archive(strip):
    issued = datetime.fromisoformat(strip.generated_at_iso)
    points = []
    if strip.synthesis is not None:
        points.append(Point('forecast_synthesis').tag('model_version', strip.model_version).time(issued)
                      .field('payload_json', strip.synthesis.model_dump_json())
                      .field('observations_json', strip.rooftop.model_dump_json())
                      .field('features_json', strip.derived.model_dump_json())
                      .field('interpreter_status',strip.interpreter_status or 'not_enabled')
                      .field('interpreter_model',strip.interpreter_model or 'none')
                      .field('interpreter_input_id',strip.interpreter_input_id or 'none')
                      .field('interpreter_attempts',strip.interpreter_attempts)
                      .field('interpreter_issues_json',json.dumps(strip.interpreter_issues)))
    for slot in strip.slots[1:]:
        if not slot.forecast_valid_time:
            continue
        valid = datetime.fromisoformat(slot.forecast_valid_time)
        if valid <= issued:
            continue
        p = (Point('six_hour_forecast').tag('model_version', strip.model_version)
             .tag('horizon', f'{slot.hour_offset}h').time(issued)
             .field('valid_time', valid.timestamp()).field('generated_at', issued.timestamp())
             .field('state', slot.state.value).field('source', slot.source)
             .field('degraded', slot.degraded).field('confidence', slot.confidence)
             .field('evidence_json', json.dumps(slot.evidence))
             .field('temperature_adjustment_c', slot.temperature_adjustment_c)
             .field('interpreter_status',strip.interpreter_status or 'not_enabled')
             .field('interpreter_model',strip.interpreter_model or 'none')
             .field('persistent_regimes_json', json.dumps([r.id for r in strip.synthesis.regimes if r.persistence == 'persistent'] if strip.synthesis else [])))
        for key, value in [('temp', slot.temperature_c), ('baseline_temp', slot.baseline_temperature_c),
                           ('precipitation', slot.precipitation_mm), ('baseline_precipitation', slot.baseline_precipitation_mm),
                           ('baseline_wind_speed', slot.baseline_wind_speed_kmh / 3.6 if slot.baseline_wind_speed_kmh is not None else None),
                           ('baseline_wind_dir', slot.baseline_wind_dir_deg),
                           ('baseline_pressure', slot.baseline_pressure_hpa), ('baseline_humidity', slot.baseline_humidity_pct),
                           ('forecast_issued', slot.forecast_issued),
                           ('wind_speed', slot.wind_speed_kmh / 3.6 if slot.wind_speed_kmh is not None else None),
                           ('pressure', slot.forecast_pressure_hpa), ('humidity', slot.forecast_humidity_pct),
                           ('wind_dir', slot.wind_dir_deg)]:
            if value is not None:
                p.field(key, float(value))
        # Simple persistence is fixed at issue time, never reconstructed using truth.
        observation_fields = [('temp', 'temperature', 'temperature_c', 1),
                              ('humidity', 'humidity', 'humidity_pct', 1),
                              ('wind_speed', 'speed', 'wind_speed_kmh', 1/3.6),
                              ('wind_dir', 'direction', 'wind_direction_deg', 1),
                              ('pressure', 'pressure', 'pressure_hpa', 1)]
        for variable, sensor_field, attribute, scale in observation_fields:
            value = getattr(strip.rooftop, attribute, None)
            if value is None or not math.isfinite(value) or not fresh(strip.rooftop, sensor_field, issued):
                continue
            if variable == 'humidity' and strip.rooftop.humidity_quality != 'ok':
                continue
            if variable == 'wind_dir' and (strip.rooftop.wind_speed_kmh is None or strip.rooftop.wind_speed_kmh < 8 or not fresh(strip.rooftop, 'speed', issued)):
                continue
            p.field('persistence_'+variable, float(value*scale))
            p.field('persistence_'+variable+'_time', datetime.fromisoformat(strip.rooftop.observation_times[sensor_field]).timestamp())
        points.append(p)
        # Maintain the existing Grafana measurement/field interface at valid time.
        for measurement in ('corrected_forecast', 'forecast_aligned'):
            display = (Point(measurement).tag('source', 'metno').tag('horizon', f'{slot.hour_offset}h')
                       .tag('model_version', strip.model_version).time(valid))
            display_values = {'temp': slot.temperature_c if measurement == 'corrected_forecast' else slot.baseline_temperature_c,
                'wind_speed': slot.wind_speed_kmh / 3.6 if slot.wind_speed_kmh is not None else None,
                'wind_dir': slot.wind_dir_deg, 'pressure': slot.forecast_pressure_hpa,
                'humidity': slot.forecast_humidity_pct, 'precipitation': slot.precipitation_mm}
            present = False
            for key, value in display_values.items():
                if value is not None:
                    display.field(key, float(value))
                    present = True
            if present:
                points.append(display)
        feature = (Point('intermediate_features').tag('source', 'metno').tag('horizon', f'{slot.hour_offset}h')
                   .tag('model_version', strip.model_version).time(issued))
        feature_values = {'pressure_gradient': strip.derived.pressure_delta_3h / 3 if strip.derived.pressure_delta_3h is not None else None,
                          'temp_trend': strip.derived.temp_delta_1h,
                          'wind_shift_indicator': float(strip.derived.wind_dir_change_1h >= 45) if strip.derived.wind_dir_change_1h is not None else None}
        if any(value is not None for value in feature_values.values()):
            for key, value in feature_values.items():
                if value is not None:
                    feature.field(key, float(value))
            points.append(feature)
    if points:
        with _get_client() as client:
            with client.write_api(write_options=SYNCHRONOUS) as writer:
                writer.write(bucket='forecast_derived', org=INFLUX_ORG, record=points)
    temporary = CACHE.with_suffix('.tmp')
    temporary.write_text(strip.model_dump_json())
    temporary.replace(CACHE)
    logging.info('Wrote %d archive/display/feature points', len(points))


def generate():
    """Only the successful atomic snapshot advances regime memory; API reads do not."""
    import fcntl
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info('A forecast update is already running')
            return
        try:
            previous = ForecastStrip.model_validate_json(CACHE.read_text()).synthesis
        except (OSError, ValueError, TypeError):
            previous = None
        archive(_build_forecast(previous_synthesis=previous))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    generate()
