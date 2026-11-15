"""Conservative, inspectable local adjustments. Scores are not probabilities."""
import math
from derived_features import dew_point


def interpret(snapshot, features):
    evidence = []
    p1, p3 = features.pressure_delta_1h, features.pressure_delta_3h
    moist = features.dewpoint_delta_1h
    if p3 is None:
        evidence.append('Pressure trend unavailable: insufficient recent coverage')
    elif p3 <= -1:
        evidence.append(f'Pressure fell {abs(p3):.1f} hPa over three hours; supports approaching unsettled weather, not a rain arrival time')
        if p1 is not None and p1 < p3 / 3 - 0.5:
            evidence.append('Pressure fall is accelerating')
        if moist is not None and moist >= 0.5:
            evidence.append('Dew point is rising too: increasing moisture corroborates deterioration')
    elif p3 >= 1:
        evidence.append(f'Pressure rose {p3:.1f} hPa over three hours; showers can still persist')
    else:
        evidence.append('Three-hour pressure trend is relatively steady')
    if (p1 is not None and p3 is not None and p1 >= 0.5 and
            features.temp_delta_1h is not None and features.temp_delta_1h <= -1 and
            features.wind_dir_change_1h is not None and features.wind_dir_change_1h >= 45):
        evidence.append('Pressure rise, cooling and wind shift support a recent frontal passage')
    h, t = features.humidity_delta_1h, features.temp_delta_1h
    if h is not None and t is not None and h >= 3 and t < 0 and (moist is None or abs(moist) < 0.5):
        evidence.append('Rising relative humidity is consistent with cooling; not independent evidence of rain')
    dp = dew_point(snapshot.temperature_c, snapshot.humidity_pct)
    snapshot.dewpoint_c = dp
    if (dp is not None and snapshot.temperature_c - dp <= 2 and
            snapshot.wind_speed_kmh is not None and snapshot.wind_speed_kmh <= 8):
        evidence.append('Small temperature/dew-point spread and light wind support mist or low-cloud potential; fog is not confirmed')
    if snapshot.rain_quality == 'ok':
        evidence.append('Recent gauge rainfall observed' if snapshot.rain_detected else 'Recent complete gauge interval was dry')
    else:
        evidence.append('Recent gauge rainfall unavailable; missing is not dry')
    return evidence


def adjust_temperature(snapshot, features, current_model, future_model, lead_hours):
    """Decaying present offset capped at +/-3 C; disabled across frontal changes.

    Fixed conservative engineering defaults, pending archived forecast evaluation.
    """
    base = future_model.get('temp')
    obs, current = snapshot.temperature_c, current_model.get('temp')
    if base is None or obs is None or current is None:
        return base, 0.0, 'No valid paired local/model temperature for correction'
    p3, shift, cooling = features.pressure_delta_3h, features.wind_dir_change_1h, features.temp_delta_1h
    if (p3 is None or cooling is None or
            (abs(p3) >= 3 or (shift is not None and shift >= 45 and abs(cooling) >= 1))):
        return base, 0.0, 'Temperature correction withheld: trend coverage missing or frontal transition possible'
    if current_model.get('wind_dir') is not None and future_model.get('wind_dir') is not None:
        if abs((future_model['wind_dir'] - current_model['wind_dir'] + 180) % 360 - 180) >= 45:
            return base, 0.0, 'Temperature correction withheld across forecast wind shift'
    offset = max(-3.0, min(3.0, obs - current)) * math.exp(-lead_hours / 2)
    return base + offset, offset, f'Local temperature offset decays with lead time ({offset:+.1f} C); regional rain forecast retained'
