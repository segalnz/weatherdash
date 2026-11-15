"""Verify genuinely issued forecasts against matched observations, never train a model.

Forecasts are archived at generation time by snapshot_forecast.py. Instantaneous
values match a nearby observation; precipitation matches [valid, valid + 1h).
Incomplete observations are excluded rather than replaced with zero.
"""
import logging
import math
import os
from datetime import datetime, timezone, timedelta
import pandas as pd
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = os.getenv('INFLUX_URL', 'http://127.0.0.1:8086')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN', '')
INFLUX_ORG = os.getenv('INFLUX_ORG', 'weather')


def read_frame(client, bucket, measurement, start, stop):
    query = (f'from(bucket: "{bucket}") |> range(start: {start.isoformat()}, stop: {stop.isoformat()}) '
             f'|> filter(fn: (r) => r._measurement == "{measurement}") '
             '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")')
    rows = [r.values for table in client.query_api().query(query, org=INFLUX_ORG) for r in table.records]
    return pd.DataFrame(rows)


def finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def rain_total(rain, valid):
    if rain.empty or not {'_time', 'rainfall_mm', 'slots_missing'}.issubset(rain):
        return None
    rows = rain.loc[(rain['_time'] >= valid) & (rain['_time'] < valid + timedelta(hours=1))].copy()
    rows = rows.drop_duplicates('_time').sort_values('_time')
    expected = pd.date_range(valid, periods=6, freq='10min')
    if (len(rows) != 6 or set(rows['_time']) != set(expected)
            or not (rows.slots_missing == 0).all()
            or not rows.rainfall_mm.map(finite).all() or (rows.rainfall_mm < 0).any()):
        return None
    return float(rows.rainfall_mm.sum())


def verify(row, observations, rain, now):
    valid = datetime.fromtimestamp(float(row['valid_time']), timezone.utc)
    issued = pd.Timestamp(row['_time']).to_pydatetime()
    if valid <= issued or valid > now:
        return {}
    errors = {}
    mapping = {'temp': ('temperature', 'sht45/telemetry'), 'wind_speed': ('speed', 'status/weather/data'),
               'wind_dir': ('direction', 'status/weather/data'), 'pressure': ('pressure', 'pressure/telemetry'),
               'humidity': ('humidity', 'sht45/telemetry')}
    for forecast_field, (field, topic) in mapping.items():
        if observations.empty or not {field, '_time'}.issubset(observations):
            continue
        if forecast_field == 'pressure':
            required = {'topic', 'station', 'quality_valid', 'pressure_basis'}
            if not required.issubset(observations):
                continue
            obs = observations.loc[(observations.topic == topic) &
                                   (observations.station == 'local-pressure')].copy()
            # Select latest observation at/before valid time, including invalid events.
            # Never skip a newer invalid packet to resurrect old good pressure.
            obs = obs[(obs['_time'] <= valid) & (obs['_time'] > valid - timedelta(minutes=10))]
            if obs.empty:
                continue
            nearest = obs['_time'].idxmax()
            if (obs.loc[nearest, 'quality_valid'] != True or
                    obs.loc[nearest, 'pressure_basis'] != 'legacy_sea_level_corrected' or
                    not finite(obs.loc[nearest, field])):
                continue
        else:
            obs = observations.loc[observations[field].map(finite)].copy()
            if 'topic' in obs:
                obs = obs[obs.topic == topic]
            if obs.empty:
                continue
            distances = (obs['_time'] - valid).abs()
            nearest = distances.idxmin()
            if distances.loc[nearest] > timedelta(minutes=10):
                continue
        actual = float(obs.loc[nearest, field])
        if finite(row.get(forecast_field)):
            error = actual - float(row[forecast_field])
            if forecast_field == 'wind_dir':
                error = (error + 180) % 360 - 180
            errors[f'{forecast_field}_error'] = error
        if forecast_field == 'temp' and finite(row.get('baseline_temp')):
            errors['baseline_temp_error'] = actual - float(row['baseline_temp'])
    if valid + timedelta(hours=1, minutes=5) <= now and finite(row.get('precipitation')):
        actual = rain_total(rain, valid)
        if actual is not None:
            errors['precipitation_error'] = actual - float(row['precipitation'])
    return errors


def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=72)
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        forecasts = read_frame(client, 'forecast_derived', 'six_hour_forecast', start, now)
        if forecasts.empty:
            logging.info('No archived issued forecasts yet; no retrospective forecasts fabricated')
            return
        observations = read_frame(client, 'weatherdata', 'mqtt_consumer', start, now)
        rain = read_frame(client, 'weatherdata', 'rainfall', start, now)
        points = []
        for _, row in forecasts.iterrows():
            errors = verify(row, observations, rain, now)
            if not errors:
                continue
            p = (Point('forecast_verification').tag('horizon', row['horizon'])
                 .tag('model_version', row['model_version']).time(row['_time'])
                 .field('valid_time', float(row['valid_time'])))
            for name, value in errors.items():
                p.field(name, value)
            points.append(p)
            legacy = (Point('forecast_error').tag('source', 'metno').tag('horizon', row['horizon'])
                      .tag('model_version', row['model_version'])
                      .time(datetime.fromtimestamp(float(row['valid_time']), timezone.utc)))
            for name, value in errors.items():
                legacy.field(name, value)
            points.append(legacy)
        if points:
            with client.write_api(write_options=SYNCHRONOUS) as writer:
                writer.write(bucket='forecast_derived', org=INFLUX_ORG, record=points)
        logging.info('Verified %d archived forecasts', len(points))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
