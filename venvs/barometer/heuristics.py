"""Observation classification and conservative regional hourly forecasts.

Humidity and pressure trends do not establish rainfall or its arrival time.
"""

from forecast_state import (
    AtmosphericState,
    PressureTendency,
    DerivedFeatures,
    RooftopSnapshot,
)

STATE_LABELS_SIMPLE: dict[AtmosphericState, str] = {
    AtmosphericState.STABLE: "Settled fine",
    AtmosphericState.SUNNY: "Sunny",
    AtmosphericState.MOSTLY_SUNNY: "Mostly sunny",
    AtmosphericState.PARTLY_CLOUDY: "Partly cloudy",
    AtmosphericState.CLOUDY: "Cloudy",
    AtmosphericState.OVERCAST: "Overcast",
    AtmosphericState.DRIZZLE: "Light rain",
    AtmosphericState.SHOWERS: "Showers",
    AtmosphericState.RAIN_LIKELY: "Rain",
    AtmosphericState.HEAVY_RAIN: "Heavy rain",
    AtmosphericState.WINDY: "Windy",
    AtmosphericState.FOGGY: "Foggy",
}

STATE_ICONS_SIMPLE: dict[AtmosphericState, str] = {
    AtmosphericState.STABLE: "\u2600\ufe0f",
    AtmosphericState.SUNNY: "\u2600\ufe0f",
    AtmosphericState.MOSTLY_SUNNY: "\U0001f324\ufe0f",
    AtmosphericState.PARTLY_CLOUDY: "\u26c5",
    AtmosphericState.CLOUDY: "\u2601\ufe0f",
    AtmosphericState.OVERCAST: "\u2601\ufe0f",
    AtmosphericState.DRIZZLE: "\u2614",
    AtmosphericState.SHOWERS: "\U0001f326\ufe0f",
    AtmosphericState.RAIN_LIKELY: "\U0001f327\ufe0f",
    AtmosphericState.HEAVY_RAIN: "\U0001f327\ufe0f",
    AtmosphericState.WINDY: "\U0001f4a8",
    AtmosphericState.FOGGY: "\U0001f32b\ufe0f",
}

STATE_COLORS: dict[AtmosphericState, str] = {
    AtmosphericState.STABLE: "#fbbf24",
    AtmosphericState.SUNNY: "#fbbf24",
    AtmosphericState.MOSTLY_SUNNY: "#fcd34d",
    AtmosphericState.PARTLY_CLOUDY: "#94a3b8",
    AtmosphericState.CLOUDY: "#64748b",
    AtmosphericState.OVERCAST: "#475569",
    AtmosphericState.DRIZZLE: "#60a5fa",
    AtmosphericState.SHOWERS: "#3b82f6",
    AtmosphericState.RAIN_LIKELY: "#2563eb",
    AtmosphericState.HEAVY_RAIN: "#1d4ed8",
    AtmosphericState.WINDY: "#f59e0b",
    AtmosphericState.FOGGY: "#94a3b8",
}


class NowForecast:
    """Current observation-derived forecast with a gradual trend projection."""

    def __init__(
        self,
        state: AtmosphericState,
        label: str,
        confidence: int,
        pressure_tendency: PressureTendency,
        trend_6h: str = "",
    ):
        self.state = state
        self.label = label
        self.confidence = confidence
        self.pressure_tendency = pressure_tendency
        self.trend_6h = trend_6h


def _classify_cloud(rooftop: RooftopSnapshot) -> tuple[AtmosphericState, str, int]:
    h = rooftop.humidity_pct if rooftop.humidity_quality not in ("invalid", "possible_condensation") else None
    lux = rooftop.lux
    press = rooftop.pressure_hpa
    hour = _hour()

    is_dawn = hour < 9

    if is_dawn and h is not None and h > 70 and press is not None and press > 1020:
        h = None

    if press is not None and press > 1022 and (h is None or h < 70):
        if lux is not None:
            if lux > 8000:
                return AtmosphericState.STABLE, "Settled fine", 90
            elif lux > 2000:
                return AtmosphericState.SUNNY, "Sunny", 85
            elif lux > 400:
                return AtmosphericState.MOSTLY_SUNNY, "Mostly sunny", 80
            elif press > 1024 and (rooftop.wind_speed_kmh is None or rooftop.wind_speed_kmh < 5):
                return AtmosphericState.STABLE, "Settled fine", 85
            elif 8 <= hour <= 18:
                return AtmosphericState.CLOUDY, "Cloudy", 70
        return AtmosphericState.STABLE, "Settled fine", 85

    if press is not None and press > 1018:
        if h is not None:
            if h < 60:
                return AtmosphericState.STABLE, "Settled fine", 85
            elif h < 75:
                return AtmosphericState.MOSTLY_SUNNY, "Mostly sunny", 80
            elif h < 85:
                return AtmosphericState.PARTLY_CLOUDY, "Partly cloudy", 75
            else:
                return AtmosphericState.CLOUDY, "Cloudy", 70
        return AtmosphericState.STABLE, "Settled fine", 85

    if press is not None and press < 1010:
        if h is not None:
            if h > 85:
                return AtmosphericState.CLOUDY, "Cloudy", 60
            if h < 80:
                if lux is not None:
                    if lux > 8000:
                        return AtmosphericState.SUNNY, "Sunny", 85
                    if lux > 2000:
                        return AtmosphericState.MOSTLY_SUNNY, "Mostly sunny", 80
                return AtmosphericState.MOSTLY_SUNNY, "Mostly sunny", 80
            return AtmosphericState.PARTLY_CLOUDY, "Partly cloudy", 75
        return AtmosphericState.CLOUDY, "Cloudy", 65

    if h is not None and h > 90:
        if lux is not None and lux < 200 and 8 <= _hour() <= 18:
            return AtmosphericState.OVERCAST, "Overcast", 60
        return AtmosphericState.CLOUDY, "Cloudy", 55

    if h is not None and h > 85:
        return AtmosphericState.CLOUDY, "Cloudy", 65

    if h is not None and h < 60:
        return AtmosphericState.MOSTLY_SUNNY, "Mostly sunny", 75

    return AtmosphericState.PARTLY_CLOUDY, "Partly cloudy", 70


def _hour() -> int:
    from datetime import datetime, timezone, timedelta
    import zoneinfo
    try:
        nz = zoneinfo.ZoneInfo("Pacific/Auckland")
        return datetime.now(timezone.utc).astimezone(nz).hour
    except Exception:
        return 12


def compute_now_forecast(
    rooftop: RooftopSnapshot,
    derived: DerivedFeatures,
) -> NowForecast:
    state, label, confidence = AtmosphericState.UNKNOWN, "Cloud cover unobserved", 40
    pt = compute_pressure_tendency(derived)

    from local_forecast import interpret
    trend_6h = "; ".join(interpret(rooftop, derived))

    if rooftop.wind_speed_kmh is not None and rooftop.wind_speed_kmh > 25:
        trend_6h = "Windy"
        state = AtmosphericState.WINDY
        label = "Windy"
        confidence = 65

    if rooftop.rain_detected and state not in (AtmosphericState.SHOWERS, AtmosphericState.RAIN_LIKELY):
        state = AtmosphericState.DRIZZLE
        label = "Recent rain"
        confidence = 70

    if rooftop.wind_speed_kmh is not None and rooftop.wind_speed_kmh < 8 and state == AtmosphericState.STABLE:
        confidence = min(95, confidence + 5)

    if rooftop.humidity_quality != "ok" or rooftop.rain_quality != "ok":
        confidence = min(confidence, 55)
    return NowForecast(state, label, max(30, min(95, confidence)), pt, trend_6h)


def _project_horizon(
    now: NowForecast,
    derived: DerivedFeatures,
    horizon_h: int,
    api_forecast: dict | None = None,
) -> tuple[AtmosphericState, str, str]:
    """Use valid-time regional precipitation; local trends cannot establish rain timing.

    Missing or legacy precipitation intervals remain unknown, not dry. A
    six-hour accumulation cannot determine which individual hour will be wet.
    Confidence values elsewhere remain heuristic, not calibrated probabilities.
    """
    import math
    raw = api_forecast or {}
    amount = raw.get("precipitation")
    period = raw.get("precipitation_period_hours")
    if (amount is None or not math.isfinite(amount) or amount < 0 or period != 1):
        return AtmosphericState.UNKNOWN, "Forecast unavailable", "Missing hourly precipitation"
    if amount > 0:
        return AtmosphericState.RAIN_LIKELY, "Rain expected", "MET Norway hourly precipitation"

    # Prefer the model's cloud information for a dry future hour.
    symbol = str(raw.get("symbol_code", "")).split("_")[0]
    dry_symbols = {
        "clearsky": AtmosphericState.STABLE,
        "fair": AtmosphericState.MOSTLY_SUNNY,
        "partlycloudy": AtmosphericState.PARTLY_CLOUDY,
        "cloudy": AtmosphericState.CLOUDY,
        "fog": AtmosphericState.FOGGY,
    }
    state = dry_symbols.get(symbol)
    if state is not None:
        return state, STATE_LABELS_SIMPLE[state], "MET Norway dry hour"
    # Do not carry current rain forward or invent a cloud classification.
    return AtmosphericState.UNKNOWN, "No rain forecast", "MET Norway dry hour; cloud unknown"


def compute_pressure_tendency(derived: DerivedFeatures) -> PressureTendency:
    d3 = derived.pressure_delta_3h
    if d3 is None:
        return PressureTendency.UNKNOWN
    if d3 < -3.0:
        return PressureTendency.RAPID_FALL
    elif d3 < -1.0:
        return PressureTendency.SLOW_FALL
    elif d3 > 2.0:
        return PressureTendency.RISING
    else:
        return PressureTendency.STABLE
