def _trend_value(value):
    return "unknown" if value is None else f"{value:.1f}"

"""Legacy prompt compatibility fixture; NOT the runtime interpreter.

Called AFTER the deterministic Zambretti engine.  The LLM reviews the
same sensor data and provides structured refinements.  If the LLM is
unavailable or slow, the deterministic forecast stands unchanged.

To enable: set LLM_API_KEY and LLM_MODEL (e.g. OPENAI_API_KEY / gpt-4o)
in the .env file.  If unset, the module returns None (no refinement).
"""

import json
import os
import logging

from forecast_state import (
    RooftopSnapshot,
    DerivedFeatures,
)

log = logging.getLogger(__name__)


def build_llm_prompt(
    rooftop: RooftopSnapshot,
    derived: DerivedFeatures,
    lux: float | None,
    lux_condition: str | None,
    zambretti_letter: str,
    zambretti_label: str,
    zambretti_idx: int,
    current_confidence: int,
    month: int,
    hour: int,
    forecast_temps: list[float | None],
    forecast_winds: list[float | None],
    forecast_precip: list[float | None],
) -> list[dict]:
    """Build a structured prompt for the LLM."""

    season = _season_name(month)

    system_prompt = (
        "You are an experienced New Zealand meteorologist reviewing a "
        "barometric forecast for Moonshine Hill, near Wellington (elevation ~350m). "
        "Your task is to sanity-check the automated forecast and suggest refinements. "
        "Be concise. Think step by step.\n\n"
        "Rules:\n"
        "1. Treat the automated forecast as an unverified hypothesis. Compare it with "
        "the regional forecast and quality-qualified observations.\n"
        "2. Consider microclimate effects: Moonshine Hill is higher and inland from "
        "Wellington harbour. Conditions can differ from the MET Norway grid point.\n"
        "3. Sunshine now does not rule out rain later. High humidity or falling "
        "pressure alone does not establish rain or its arrival time.\n"
        "4. Near-saturation humidity may reflect mist, dew, or sensor condensation. "
        "Respect quality flags; missing precipitation is not zero precipitation.\n"
        "5. Respond ONLY with valid JSON, no other text.\n\n"
        "Return JSON with this exact structure:\n"
        '{\n'
        '  "assessment": "plausible" | "needs_review",\n'
        '  "rationale": "Short 1-2 sentence explanation",\n'
        '  "suggested_letter": null | "A"-"Z",\n'
        '  "confidence_delta": 0 | -5 | -10 | 5 | 10,\n'
        '  "narrative": "One sentence forecast summary in plain English"\n'
        '}'
    )

    now_obs = (
        f"## Current Observations\n"
        f"- Temperature: {rooftop.temperature_c if rooftop.temperature_c is not None else 'N/A'}°C\n"
        f"- Humidity: {rooftop.humidity_pct if rooftop.humidity_pct is not None else 'N/A'}%\n"
        f"- Pressure: {rooftop.pressure_hpa if rooftop.pressure_hpa is not None else 'N/A'} hPa\n"
        f"- Pressure trend (3h): {_trend_value(derived.pressure_delta_3h)} hPa\n"
        f"- Wind: {rooftop.wind_direction_deg if rooftop.wind_direction_deg is not None else 'N/A'}° at {rooftop.wind_speed_kmh if rooftop.wind_speed_kmh is not None else 'N/A'} km/h\n"
        f"- Rain detected: {rooftop.rain_detected} (quality: {rooftop.rain_quality})\n"
        f"- Humidity quality: {rooftop.humidity_quality}; {rooftop.humidity_quality_reason}\n"
        f"- Ambient light: {lux if lux is not None else 'N/A'} lux ({lux_condition or 'N/A'})\n"
    )

    derived_info = (
        f"## Derived Features\n"
        f"- Pressure delta 1h: {_trend_value(derived.pressure_delta_1h)} hPa\n"
        f"- Pressure delta 3h: {_trend_value(derived.pressure_delta_3h)} hPa\n"
        f"- Humidity delta 1h: {_trend_value(derived.humidity_delta_1h)}%\n"
        f"- Wind speed delta 1h: {_trend_value(derived.wind_speed_delta_1h)} km/h\n"
        f"- Wind dir change 1h: {_trend_value(derived.wind_dir_change_1h)}°\n"
        f"- Rain persistence 1h: {_trend_value(derived.rain_persistence_1h)}\n"
    )

    forecast_info = (
        f"## MET Norway 6-Hour Forecast\n"
    )
    for i in range(6):
        t = forecast_temps[i] if i < len(forecast_temps) else None
        w = forecast_winds[i] if i < len(forecast_winds) else None
        p = forecast_precip[i] if i < len(forecast_precip) else None
        forecast_info += f"- +{i+1}h: {t if t is not None else 'N/A'}°C, wind {w if w is not None else 'N/A'} km/h, precip {p if p is not None else 'N/A'} mm\n"

    zambretti_info = (
        f"## Zambretti Result\n"
        f"- Letter: {zambretti_letter} (index {zambretti_idx})\n"
        f"- State: {zambretti_label}\n"
        f"- Confidence: {current_confidence}%\n"
    )

    context_info = (
        f"## Context\n"
        f"- Season: {season}\n"
        f"- Local time: {hour:02d}:00\n"
        f"- Location: Moonshine Hill, 575 Moonshine Hill Road, elevation ~350m\n"
    )

    prompt = (now_obs + derived_info + forecast_info + zambretti_info + context_info +
              "\n## Review the forecast above. Return JSON.")

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]


def _season_name(month: int) -> str:
    if month in (12, 1, 2):
        return "Summer"
    elif month in (3, 4, 5):
        return "Autumn"
    elif month in (6, 7, 8):
        return "Winter"
    return "Spring"


def call_llm(messages: list[dict]) -> dict | None:
    """Disabled legacy adapter. Runtime interpretation lives in runtime_llm.py."""
    raise RuntimeError("Legacy refinement is disabled; use runtime_llm with validation and revision")
