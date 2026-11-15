import math

import numpy as np

from forecast_state import DerivedFeatures, Scores


def compute_confidence(
    scores: Scores | None,
    derived: DerivedFeatures,
    forecast_temp_values: list[float] | None = None,
) -> int:
    if forecast_temp_values and len(forecast_temp_values) >= 2:
        variance = float(np.std(forecast_temp_values))
        confidence = 100.0 - (variance * 10.0)
    else:
        confidence = 70.0

    if derived.pressure_delta_3h is not None and derived.pressure_delta_3h < -2.0:
        confidence -= 15

    if derived.humidity_delta_1h is not None and derived.humidity_delta_1h > 8:
        confidence -= 10

    if derived.wind_speed_delta_1h is not None and derived.wind_speed_delta_1h > 10:
        confidence -= 10

    confidence = max(0.0, min(confidence, 100.0))

    return int(round(confidence))
