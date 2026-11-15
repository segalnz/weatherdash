"""Conservative quality flags; saturation is not proof of a faulty sensor."""
import math
from datetime import datetime, timezone

import pandas as pd
from forecast_state import RooftopSnapshot


def assess_humidity(snapshot: RooftopSnapshot, history: pd.DataFrame,
                    now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    value = snapshot.humidity_pct
    snapshot.humidity_quality = "unknown"
    snapshot.humidity_quality_reason = "No recent humidity reading"
    if value is None:
        return
    if not math.isfinite(value) or not 0 <= value <= 100:
        snapshot.humidity_quality = "invalid"
        snapshot.humidity_quality_reason = "Humidity outside physical range"
        snapshot.humidity_pct = None
        return
    snapshot.humidity_quality = "ok"
    snapshot.humidity_quality_reason = None
    if value < 98:
        return
    snapshot.humidity_quality = "possible_condensation"
    snapshot.humidity_quality_reason = (
        "Near saturation: real mist/dew or sensor wetting possible; not evidence of rain"
    )
    if history.empty or not {"_time", "humidity"}.issubset(history.columns):
        return
    recent = history.loc[(history["_time"] >= now - pd.Timedelta(minutes=65)) &
                         (history["_time"] <= now), ["_time", "humidity"]].dropna().sort_values("_time")
    if (len(recent) >= 10 and recent["humidity"].between(98, 100).all()
            and recent["_time"].iloc[-1] >= now - pd.Timedelta(minutes=10)
            and recent["_time"].iloc[-1] - recent["_time"].iloc[0] >= pd.Timedelta(minutes=50)
            and recent["_time"].diff().dropna().max() <= pd.Timedelta(minutes=10)):
        snapshot.humidity_quality_reason = (
            "Sustained near saturation for at least 50 minutes: mist/dew or slow recovery "
            "from sensor wetting possible; not evidence of rain"
        )
