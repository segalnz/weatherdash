"""Conservative, inspectable local adjustments. Scores are not probabilities."""
import math
from derived_features import dew_point


def interpret(snapshot, features):
    """Compatibility text view of the structured evidence ledger."""
    from datetime import datetime, timezone
    from synthesis import build_evidence
    return [e.message for e in build_evidence(snapshot, features, datetime.now(timezone.utc))]


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
