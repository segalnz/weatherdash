"""Time-aware meteorological features; missing coverage is unknown, never stable."""
import math
import numpy as np
import pandas as pd
from forecast_state import DerivedFeatures


def compute_angular_difference(a, b):
    return abs((a - b + 180) % 360 - 180)


def circular_mean(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return float('nan')
    z = np.exp(1j * np.radians(values)).mean()
    return float(np.degrees(np.angle(z)) % 360) if abs(z) > 0.1 else float('nan')


def dew_point(temp, humidity):
    if temp is None or humidity is None or not (math.isfinite(temp) and 0 < humidity <= 100):
        return None
    gamma = math.log(humidity / 100) + 17.625 * temp / (243.04 + temp)
    return 243.04 * gamma / (17.625 - gamma)


def trend(df, field, hours, now):
    """OLS change across requested duration, requiring endpoints and gap coverage."""
    if df.empty or field not in df:
        return None
    start = pd.Timestamp(now) - pd.Timedelta(hours=hours)
    rows = df.loc[(df['_time'] >= start) & (df['_time'] <= now), ['_time', field]].dropna().sort_values('_time')
    rows[field] = pd.to_numeric(rows[field], errors='coerce').astype(float)
    rows = rows[np.isfinite(rows[field])]
    tolerance = pd.Timedelta(minutes=15)
    if (len(rows) < max(4, int(hours * 6)) or rows['_time'].iloc[0] > start + tolerance
            or rows['_time'].iloc[-1] < pd.Timestamp(now) - tolerance
            or rows['_time'].diff().dropna().max() > tolerance):
        return None
    x = (rows['_time'] - rows['_time'].iloc[0]).dt.total_seconds().to_numpy() / 3600
    if x[-1] < hours * 0.7:
        return None
    coefficients = np.polyfit(x, rows[field].to_numpy(), 1)
    if field == 'pressure':
        residual = rows[field].to_numpy() - np.polyval(coefficients, x)
        # Conservative sensor-quality limits, not a storm detector.
        if float(np.sqrt(np.mean(residual ** 2))) > 1.2:
            return None
        if ((rows[field].diff().abs() > 3) & (rows['_time'].diff() <= pd.Timedelta(minutes=10))).any():
            return None
    return float(coefficients[0] * hours)


def compute_derived_features(rooftop_df, rain_df, now=None):
    now = pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    result = DerivedFeatures()
    for output, field, hours in [
        ('pressure_delta_1h', 'pressure', 1), ('pressure_delta_3h', 'pressure', 3),
        ('humidity_delta_1h', 'humidity', 1), ('temp_delta_1h', 'temperature', 1),
        ('wind_speed_delta_1h', 'speed', 1)]:
        setattr(result, output, trend(rooftop_df, field, hours, now))
    if not rooftop_df.empty:
        if {'temperature', 'humidity'}.issubset(rooftop_df):
            df = rooftop_df.copy()
            df['dewpoint'] = [dew_point(t, h) for t, h in zip(df.temperature, df.humidity)]
            result.dewpoint_delta_1h = trend(df, 'dewpoint', 1, now)
        if 'direction' in rooftop_df:
            def window_mean(start, stop):
                return circular_mean(rooftop_df.loc[rooftop_df['_time'].between(start, stop), 'direction'])
            a = window_mean(now - pd.Timedelta(minutes=70), now - pd.Timedelta(minutes=50))
            b = window_mean(now - pd.Timedelta(minutes=15), now)
            if math.isfinite(a) and math.isfinite(b):
                result.wind_dir_change_1h = compute_angular_difference(a, b)
    if not rain_df.empty:
        recent = rain_df.loc[rain_df['_time'].between(now - pd.Timedelta(minutes=70), now - pd.Timedelta(minutes=10))].dropna(subset=['rainfall_mm'])
        if len(recent) >= 5:
            result.rain_persistence_1h = float((recent.rainfall_mm > 0).mean())
    return result
