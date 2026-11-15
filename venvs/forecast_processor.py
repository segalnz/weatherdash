"""Verify issued forecasts and their issue-time baselines, never train a model."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import json
import logging
import math
import os
from pathlib import Path
import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = os.getenv('INFLUX_URL', 'http://127.0.0.1:8086')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN', '')
INFLUX_ORG = os.getenv('INFLUX_ORG', 'weather')
POLICY = 'verification_v2'
WET_MM = 0.1
CALM_MS = 8 / 3.6
REPORT = Path(os.getenv('FORECAST_VERIFICATION_REPORT', '/var/lib/weatherdash/verification_report.json'))
SOURCES = {'temp': ('temperature', 'sht45/telemetry'),
           'humidity': ('humidity', 'sht45/telemetry'),
           'wind_speed': ('speed', 'status/weather/data'),
           'wind_dir': ('direction', 'status/weather/data'),
           'pressure': ('pressure', 'pressure/telemetry')}
UNITS = {'temp': 'C', 'humidity': '%', 'wind_speed': 'm/s', 'wind_dir': 'degrees', 'pressure': 'hPa', 'precipitation': 'mm/hour'}


def read_frame(client, bucket, measurement, start, stop, station=None):
    station_filter = f' and r.station == "{station}"' if station else ''
    query = (f'from(bucket: "{bucket}") |> range(start: {start.isoformat()}, stop: {stop.isoformat()}) '
             f'|> filter(fn: (r) => r._measurement == "{measurement}"{station_filter}) '
             '|> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")')
    return pd.DataFrame([r.values for table in client.query_api().query(query, org=INFLUX_ORG) for r in table.records])


def finite(value):
    try:
        return not isinstance(value, (bool, np.bool_)) and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def valid_flag(value):
    return isinstance(value, (bool, np.bool_)) and bool(value)


class ObservationMatcher:
    """Index once; latest at/before valid time includes invalid packets."""
    def __init__(self, frame):
        self.topics = {}
        if not {'_time', 'topic'}.issubset(frame):
            return
        frame = frame.copy()
        frame['_time'] = pd.to_datetime(frame['_time'], utc=True, errors='coerce')
        for topic, rows in frame.dropna(subset=['_time']).groupby('topic'):
            if topic == 'pressure/telemetry':
                if 'station' not in rows:
                    continue
                rows = rows[rows.station == 'local-pressure']
            self.topics[topic] = rows.sort_values('_time').reset_index(drop=True)

    def actual(self, variable, valid):
        field, topic = SOURCES[variable]
        rows = self.topics.get(topic)
        if rows is None or rows.empty or field not in rows:
            return None
        index = rows['_time'].searchsorted(pd.Timestamp(valid), side='right') - 1
        if index < 0:
            return None
        row = rows.iloc[index]
        age = (valid - row['_time']).total_seconds()
        if not 0 <= age < 600:
            return None
        # Conflicting same-time series must not choose a convenient ground truth.
        left = rows['_time'].searchsorted(row['_time'], side='left')
        ties = rows.iloc[left:index+1]
        if not ties[field].map(finite).all() or ties[field].nunique() != 1:
            return None
        if variable in ('temp', 'humidity', 'pressure'):
            if 'quality_valid' not in ties or not ties.quality_valid.map(valid_flag).all():
                return None
        if variable == 'pressure' and ('pressure_basis' not in ties or not (ties.pressure_basis == 'legacy_sea_level_corrected').all()):
            return None
        value = float(row[field])
        if ((variable == 'wind_speed' and value < 0)
                or (variable == 'wind_dir' and not 0 <= value <= 360)
                or (variable == 'temp' and not -60 <= value <= 65)
                or (variable == 'humidity' and not 0 <= value <= 100)
                or (variable == 'pressure' and not .01 <= value < 2000)):
            return None
        if variable == 'wind_dir':
            speed = self.actual('wind_speed', valid)
            if speed is None or speed[0] < CALM_MS:
                return None
        return value, age


def rain_total(rain, valid):
    if rain.empty or not {'_time', 'rainfall_mm', 'slots_missing'}.issubset(rain):
        return None
    rows = rain
    if 'station' in rows:
        rows = rows[rows.station == 'davis-rainbucket']
    rows = rows[(rows['_time'] >= valid) & (rows['_time'] < valid + timedelta(hours=1))]
    expected = pd.date_range(valid, periods=6, freq='10min')
    if (len(rows) != 6 or set(rows['_time']) != set(expected)
            or not (rows.slots_missing == 0).all()
            or not rows.rainfall_mm.map(finite).all() or (rows.rainfall_mm < 0).any()):
        return None
    return float(rows.rainfall_mm.sum())


def _error(actual, predicted, variable):
    error = actual - float(predicted)
    return (error + 180) % 360 - 180 if variable == 'wind_dir' else error


def verify(row, observations, rain, now):
    if not finite(row.get('valid_time')):
        return {}
    valid = datetime.fromtimestamp(float(row['valid_time']), timezone.utc)
    issued = pd.Timestamp(row['_time']).to_pydatetime()
    if valid <= issued or valid > now:
        return {}
    matcher = observations if isinstance(observations, ObservationMatcher) else ObservationMatcher(observations)
    result = {'lead_minutes': (valid-issued).total_seconds()/60}
    for variable in SOURCES:
        result[f'{variable}_verified'] = False
        match = matcher.actual(variable, valid)
        if match is None or not finite(row.get(variable)):
            continue
        actual, age = match
        result[f'{variable}_verified'] = True
        result[f'actual_{variable}'] = actual
        result[f'{variable}_observation_age_seconds'] = age
        result[f'{variable}_error'] = _error(actual, row[variable], variable)
        for method in ('baseline', 'persistence'):
            name = f'{method}_{variable}'
            if not finite(row.get(name)):
                continue
            if method == 'persistence':
                stamp = row.get(f'{name}_time')
                if not finite(stamp) or not 0 <= issued.timestamp()-float(stamp) < 600:
                    continue
                if variable == 'wind_dir' and (not finite(row.get('persistence_wind_speed')) or row['persistence_wind_speed'] < CALM_MS):
                    continue
            result[f'{name}_error'] = _error(actual, row[name], variable)
    result['precipitation_verified'] = False
    if valid + timedelta(hours=1, minutes=5) <= now and finite(row.get('precipitation')):
        actual = rain_total(rain, valid)
        if actual is not None:
            result['precipitation_verified'] = True
            result['actual_precipitation'] = actual
            result['precipitation_error'] = actual - float(row['precipitation'])
            result['precipitation_wet_observed'] = actual >= WET_MM
            result['precipitation_wet_predicted'] = float(row['precipitation']) >= WET_MM
            if finite(row.get('baseline_precipitation')):
                result['baseline_precipitation_error'] = actual-float(row['baseline_precipitation'])
            # An explicit always-dry reference, not invented rain persistence.
            result['dry_reference_precipitation_error'] = actual
    return result


def _metrics(values):
    return dict(count=len(values), bias=sum(values)/len(values),
                mae=sum(abs(v) for v in values)/len(values),
                rmse=math.sqrt(sum(v*v for v in values)/len(values)))


def build_report(records, now, hours):
    cohorts = defaultdict(list)
    for row, scores in records:
        if not scores:
            continue
        lead = int(math.ceil(scores['lead_minutes']/60))
        version = str(row.get('model_version', 'unknown'))
        cohorts[(version, lead, 'all')].append((row, scores))
        try:
            regimes = json.loads(row['persistent_regimes_json'])
            if not isinstance(regimes, list) or not all(isinstance(r, str) for r in regimes):
                raise ValueError('Invalid regime context')
            groups = ['persistent:'+r for r in set(regimes)] or ['no_persistent_assessment']
        except (KeyError, TypeError, ValueError):
            groups = ['unrecorded']
        for regime in groups:
            cohorts[(version, lead, regime)].append((row, scores))
    output = []
    for (version, lead, regime), rows in sorted(cohorts.items()):
        variables = {}
        for variable in UNITS:
            methods, paired = {}, {}
            candidate_key = f'{variable}_error'
            for method, prefix in [('weatherdash',''),('metno','baseline_'),('persistence','persistence_'),('always_dry','dry_reference_')]:
                key = prefix+variable+'_error'
                values = [float(s[key]) for _,s in rows if s.get(variable+'_verified') and finite(s.get(key))]
                if values:
                    methods[method] = _metrics(values)
                if method != 'weatherdash':
                    pairs = [(float(s[candidate_key]),float(s[key])) for _,s in rows
                             if s.get(variable+'_verified') and finite(s.get(candidate_key)) and finite(s.get(key))]
                    if pairs:
                        paired[method] = dict(count=len(pairs),
                            weatherdash_mae=sum(abs(a) for a,b in pairs)/len(pairs),
                            reference_mae=sum(abs(b) for a,b in pairs)/len(pairs),
                            mae_improvement=sum(abs(b)-abs(a) for a,b in pairs)/len(pairs))
            if methods:
                variables[variable] = dict(unit=UNITS[variable], methods=methods, paired=paired)
        matrix = dict(hits=0, misses=0, false_alarms=0, correct_negatives=0)
        for _,s in rows:
            if s.get('precipitation_verified'):
                observed, predicted = s['precipitation_wet_observed'], s['precipitation_wet_predicted']
                key = 'hits' if observed and predicted else 'misses' if observed else 'false_alarms' if predicted else 'correct_negatives'
                matrix[key] += 1
        output.append(dict(model_version=version, lead_hour=lead, regime=regime, variables=variables, precipitation_contingency=matrix))
    return dict(schema_version=2, policy=POLICY, generated_at=now.isoformat(), lookback_hours=hours,
        wet_threshold_mm=WET_MM, error_sign='observation_minus_forecast', cohorts=output,
        limitations=['Forecasts from overlapping issue times are correlated; counts are not independent weather events. Regime subcohorts can overlap and must not be added together.',
                     'Positive paired MAE improvement means lower error than the reference on the same observations; it is not a significance test.',
                     'Missing issue-time baselines are not reconstructed retrospectively.',
                     'Pressure errors use the configured corrected-pressure basis and may include site reduction/calibration differences.',
                     'Front and mist hypotheses are not scored as confirmed events without independent event/visibility labels.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=int, default=72)
    parser.add_argument('--report-only', action='store_true', help='Print a recomputed report without writing Influx or report files')
    args = parser.parse_args(argv)
    if not 1 <= args.hours <= 720:
        parser.error('--hours must be between 1 and 720')
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=args.hours)
    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        forecasts = read_frame(client, 'forecast_derived', 'six_hour_forecast', start, now)
        observations = ObservationMatcher(read_frame(client, 'weatherdata', 'mqtt_consumer', start-timedelta(minutes=10), now))
        rain = read_frame(client, 'weatherdata', 'rainfall', start, now, station='davis-rainbucket')
        if not rain.empty:
            rain['_time'] = pd.to_datetime(rain['_time'], utc=True)
        records, points = [], []
        for _,row in forecasts.iterrows():
            scores = verify(row, observations, rain, now)
            if not scores:
                continue
            records.append((row, scores))
            point = (Point('forecast_verification').tag('horizon',row['horizon'])
                     .tag('model_version',row['model_version']).tag('verification_version',POLICY)
                     .time(row['_time']).field('valid_time',float(row['valid_time'])))
            for name,value in scores.items():
                point.field(name,value)
            points.append(point)
            # Retain existing dashboard error fields, with explicit eligibility flags.
            legacy = (Point('forecast_error').tag('source','metno').tag('horizon',row['horizon'])
                      .tag('model_version',row['model_version'])
                      .time(datetime.fromtimestamp(float(row['valid_time']),timezone.utc)))
            for name,value in scores.items():
                if name.endswith('_error') or name.endswith('_verified'):
                    legacy.field(name,value)
            points.append(legacy)
        report = build_report(records,now,args.hours)
        if args.report_only:
            print(json.dumps(report,indent=2,allow_nan=False))
            return report
        if points:
            with client.write_api(write_options=SYNCHRONOUS) as writer:
                writer.write(bucket='forecast_derived',org=INFLUX_ORG,record=points)
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    temporary = REPORT.with_suffix('.tmp')
    temporary.write_text(json.dumps(report,indent=2,allow_nan=False))
    temporary.replace(REPORT)
    logging.info('Verified %d issued forecasts; wrote %s',len(records),REPORT)
    return report


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
