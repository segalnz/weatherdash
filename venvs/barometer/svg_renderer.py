import math
from html import escape

from forecast_state import (
    ForecastStrip,
    ForecastSlot,
    PressureTendency,
    AtmosphericState,
    STATE_ICONS,
)

SLOT_W = 170
SLOT_H = 340
MARGIN = 24
HEADER_H = 42
GRAPH_H = 100
GRAPH_Y = HEADER_H + 4
OFFSET_Y = HEADER_H + GRAPH_H + 24
FOOTER_H = 76

BG = "#0d1117"
SLOT_BG = "#161b22"
TEXT_PRIMARY = "#ffd700"
TEXT_SECONDARY = "#00e5ff"
TEXT_MUTED = "#8b949e"
TEMP_COLOR = "#ffd700"
BAR_TRACK = "#21262d"
ARROW_COLOR = "#00e5ff"
NOW_COLOR = "#ff6b6b"
GRAPH_BG = "#161b22"
GRID_COLOR = "#21262d"
LINE_COLOR = "#00e5ff"


def render_forecast_strip(strip: ForecastStrip) -> str:
    cols = len(strip.slots)
    total_w = cols * SLOT_W + MARGIN * 2
    total_h = OFFSET_Y + SLOT_H + FOOTER_H

    tendency_arrow = _tendency_symbol(strip.pressure_tendency)
    tendency_label = _tendency_label(strip.pressure_tendency)

    lines = [f'<svg xmlns="http://www.w3.org/2000/svg"'
             f' width="{total_w}" height="{total_h}"'
             f' viewBox="0 0 {total_w} {total_h}"'
             f' style="max-width:100%;height:auto">']

    lines.append('<title>' + escape('; '.join(strip.evidence)) + '</title>')
    lines.append(f'<rect width="100%" height="100%" fill="{BG}" rx="14"/>')
    lines.append(_defs_block())

    lines.append(f'<text x="{MARGIN}" y="28" fill="{TEXT_SECONDARY}" font-family="sans-serif" font-size="15" font-weight="bold">Pressure: {tendency_arrow} {tendency_label} | observed history, last 6h</text>')

    _draw_pressure_graph(lines, strip)

    for i, slot in enumerate(strip.slots):
        x = MARGIN + i * SLOT_W
        _draw_slot(lines, x, OFFSET_Y, slot, i)

    for index, note in enumerate(strip.evidence[:2]):
        lines.append(f'<text x="{MARGIN}" y="{total_h - 48 + index * 18}" fill="{TEXT_SECONDARY}" font-family="sans-serif" font-size="12">{escape(note[:160])}</text>')
    lines.append(f'<text x="{MARGIN}" y="{total_h - 8}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="13">Moonshine Valley Barometer \u2014 {escape(strip.generated_at)} | evidence score, not rain probability</text>')
    lines.append('</svg>')
    return "\n".join(lines)


def _defs_block() -> str:
    return (
        '<defs>'
        '<marker id="arrowhead" markerWidth="9" markerHeight="7" '
        f'refX="9" refY="3.5" orient="auto">'
        f'<polygon points="0 0, 9 3.5, 0 7" fill="{ARROW_COLOR}"/>'
        '</marker>'
        '</defs>'
    )


def _draw_pressure_graph(lines: list, strip: ForecastStrip):
    gh = GRAPH_H
    gy = GRAPH_Y
    gx = MARGIN
    gw = len(strip.slots) * SLOT_W - 10

    his = strip.pressure_history
    times = strip.pressure_history_times

    finite_values = [v for v in his if v is not None and math.isfinite(v)]
    if not finite_values:
        return

    p_min = min(finite_values)
    p_max = max(finite_values)
    pad = max((p_max - p_min) * 0.3, 0.3)
    p_lo = math.floor((p_min - pad) / 1.0) * 1.0
    p_hi = math.ceil((p_max + pad) / 1.0) * 1.0
    p_range = p_hi - p_lo

    lines.append(f'<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" rx="6" fill="{GRAPH_BG}"/>')

    n_ticks = max(2, int(gh / 28))
    for i in range(n_ticks + 1):
        y_tick = gy + gh - (gh * i / n_ticks)
        val = p_lo + p_range * i / n_ticks
        lines.append(f'<line x1="{gx + 30}" y1="{y_tick:.1f}" x2="{gx + gw}" y2="{y_tick:.1f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        lines.append(f'<text x="{gx + 26}" y="{y_tick + 4}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="10" text-anchor="end" font-weight="bold">{val:.0f}</text>')

    hist_w = gw - 30
    hist_x0 = gx + 30

    from datetime import datetime
    stamps = [datetime.fromisoformat(value).timestamp() for value in strip.pressure_history_timestamps]
    def _x_hist(idx, total):
        if len(stamps) == total and total > 1 and stamps[-1] > stamps[0]:
            return hist_x0 + hist_w * (stamps[idx] - stamps[0]) / (stamps[-1] - stamps[0])
        return hist_x0 + hist_w * idx / max(total - 1, 1)

    def _y(val):
        return gy + gh - gh * (val - p_lo) / p_range

    his_n = len(his)
    # Split the line at missing/invalid observations; never bridge a gap.
    segment = []
    for i, value in enumerate(his):
        if value is not None and math.isfinite(value):
            segment.append(f"{_x_hist(i, his_n):.1f},{_y(value):.1f}")
        else:
            if len(segment) >= 2:
                pts = " ".join(segment)
                lines.append(f'<polyline points="{pts}" fill="none" stroke="{LINE_COLOR}" stroke-width="2"/>')
            segment = []
    if len(segment) >= 2:
        pts = " ".join(segment)
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{LINE_COLOR}" stroke-width="2"/>')

    for i in range(7):
        index = round((his_n - 1) * i / 6)
        dx = _x_hist(index, his_n)
        t = times[index] if index < len(times) else ""
        col = TEXT_SECONDARY if i == 6 else TEXT_MUTED
        lines.append(f'<text x="{dx:.1f}" y="{gy + gh + 12}" fill="{col}" font-family="sans-serif" font-size="9" text-anchor="middle" font-weight="bold">{t}</text>')


def _slot_icon(slot: ForecastSlot) -> str:
    day_icon = STATE_ICONS.get(slot.state, "\u2601\ufe0f")
    
    if slot.is_night:
        # Map daytime composite icons to nighttime versions (sun → moon, keep cloud/rain)
        night_mapping = {
            "\u2600\ufe0f": "\U0001f31d",  # ☀️ → 🌝 (sun → full moon)
            "\U0001f324\ufe0f": "\U0001f31b",  # 🌤️ → 🌛 (sun + small cloud → crescent moon)
            "\u26c5": "\U0001f31b",  # ⛅ → 🌛 (sun + cloud → crescent moon)
            "\U0001f325\ufe0f": "\u2601\ufe0f",  # 🌥️ → ☁️ (sun + large cloud → cloud)
            "\U0001f326\ufe0f": "\U0001f327\ufe0f",  # 🌦️ → 🌧️ (sun + cloud + rain → cloud + rain)
        }
        return night_mapping.get(day_icon, day_icon)
    return day_icon


def _draw_slot(lines: list, x: int, y: int, slot: ForecastSlot, idx: int):
    lines.append("<g><title>" + escape("; ".join(slot.evidence)) + "</title>")
    w = SLOT_W - 10
    lines.append(f'<rect x="{x}" y="{y}" width="{w}" height="{SLOT_H}" rx="10" fill="{SLOT_BG}"/>')
    cx = x + w // 2

    if idx == 0:
        lines.append(f'<text x="{cx}" y="{y + 26}" fill="{NOW_COLOR}" font-family="sans-serif" font-size="15" text-anchor="middle" font-weight="bold">{slot.time_label}</text>')
    else:
        lines.append(f'<text x="{cx}" y="{y + 26}" fill="{TEXT_PRIMARY}" font-family="sans-serif" font-size="15" text-anchor="middle">{slot.time_label}</text>')

    icon = _slot_icon(slot)
    lines.append(f'<text x="{cx}" y="{y + 68}" font-size="38" text-anchor="middle">{icon}</text>')

    label_color = _state_color(slot.state)
    _draw_wrapped_text(lines, slot.state_label, cx, y + 90, w - 16, label_color, 12)

    if slot.temperature_c is not None:
        lines.append(f'<text x="{cx}" y="{y + 135}" fill="{TEMP_COLOR}" font-family="sans-serif" font-size="20" text-anchor="middle" font-weight="bold">{slot.temperature_c:.1f}\u00b0</text>')

    wind_base_y = y + 165
    _draw_wind(lines, cx, wind_base_y, slot.wind_dir_deg, slot.wind_speed_kmh)

    conf_y = y + 285
    _draw_confidence_bar(lines, cx, conf_y, w - 24, slot.confidence)

    lines.append("</g>")


def _draw_wrapped_text(lines: list, text: str, cx: int, y: int, max_w: int, color: str, font_size: int):
    words = text.split()
    if not words:
        return

    avg_char_w = font_size * 0.6
    chars_per_line = max(1, int(max_w / avg_char_w))

    line1 = ""
    line2 = ""
    current = ""

    for word in words:
        test = current + (" " if current else "") + word
        if len(test) <= chars_per_line:
            current = test
        else:
            if not line1:
                line1 = current
                current = word
            else:
                line2 = current
                current = word
    if not line2:
        line2 = current

    if line1:
        lines.append(f'<text x="{cx}" y="{y}" fill="{color}" font-family="sans-serif" font-size="{font_size}" text-anchor="middle" font-weight="bold">{_escape(line1)}</text>')
    if line2:
        lines.append(f'<text x="{cx}" y="{y + font_size + 3}" fill="{color}" font-family="sans-serif" font-size="{font_size}" text-anchor="middle" font-weight="bold">{_escape(line2)}</text>')


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;")


WIND_CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _wind_cardinal(deg: float) -> str:
    return WIND_CARDINALS[round((deg % 360) / 22.5) % 16]


def _draw_wind(lines: list, cx: int, y: int, wind_dir: float | None, wind_speed: float | None):
    if wind_dir is not None:
        angle_rad = math.radians((wind_dir + 180) % 360 - 90)
        arm = 14
        x1 = cx - arm * math.cos(angle_rad)
        y1 = y - arm * math.sin(angle_rad)
        x2 = cx + arm * math.cos(angle_rad)
        y2 = y + arm * math.sin(angle_rad)

        lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{ARROW_COLOR}" stroke-width="3" marker-end="url(#arrowhead)"/>')

    label_base = y + 28
    if wind_dir is not None:
        lines.append(f'<text x="{cx}" y="{label_base}" fill="{ARROW_COLOR}" font-family="sans-serif" font-size="12" text-anchor="middle" font-weight="bold">{wind_dir % 360:.0f}\u00b0</text>')
        card_y = label_base + 16
        lines.append(f'<text x="{cx}" y="{card_y}" fill="{ARROW_COLOR}" font-family="sans-serif" font-size="10" text-anchor="middle" font-weight="bold">{_wind_cardinal(wind_dir)}</text>')
    if wind_speed is not None:
        spd_y = (label_base + 16 + 16) if wind_dir is not None else (label_base + 16)
        lines.append(f'<text x="{cx}" y="{spd_y}" fill="{TEXT_SECONDARY}" font-family="sans-serif" font-size="12" text-anchor="middle" font-weight="bold">{wind_speed:.1f} km/h</text>')


def _draw_confidence_bar(lines: list, cx: int, y: int, bar_w: int, confidence: int):
    bar_x = cx - bar_w // 2
    bar_h = 10

    lines.append(f'<rect x="{bar_x}" y="{y}" width="{bar_w}" height="{bar_h}" rx="5" fill="{BAR_TRACK}"/>')
    fill_w = bar_w * (confidence / 100.0)
    color = _confidence_color(confidence)
    lines.append(f'<rect x="{bar_x}" y="{y}" width="{fill_w:.1f}" height="{bar_h}" rx="5" fill="{color}"/>')
    lines.append(f'<text x="{bar_x + bar_w}" y="{y + 9}" fill="{color}" font-family="sans-serif" font-size="10" text-anchor="end" font-weight="bold">{confidence}/100</text>')


def _confidence_color(confidence: int) -> str:
    if confidence >= 70:
        return "#00e676"
    elif confidence >= 40:
        return "#ffab00"
    return "#ff1744"


def _state_color(state: AtmosphericState) -> str:
    return {
        AtmosphericState.STABLE: "#ffd700",
        AtmosphericState.SUNNY: "#ffd700",
        AtmosphericState.MOSTLY_SUNNY: "#ffe082",
        AtmosphericState.PARTLY_CLOUDY: "#b0bec5",
        AtmosphericState.CLOUDY: "#90a4ae",
        AtmosphericState.MOSTLY_CLOUDY: "#78909c",
        AtmosphericState.OVERCAST: "#607d8b",
        AtmosphericState.DRIZZLE: "#40c4ff",
        AtmosphericState.SHOWERS: "#00b0ff",
        AtmosphericState.RAIN_LIKELY: "#0091ea",
        AtmosphericState.HEAVY_RAIN: "#1565c0",
        AtmosphericState.WINDY: "#ff6d00",
        AtmosphericState.CLEARING: "#00e676",
        AtmosphericState.FOGGY: "#90a4ae",
    }.get(state, "#94a3b8")


def _tendency_symbol(tendency: PressureTendency) -> str:
    return {
        PressureTendency.RAPID_FALL: "\u2198\u2198",
        PressureTendency.SLOW_FALL: "\u2198",
        PressureTendency.STABLE: "\u2192",
        PressureTendency.RISING: "\u2197",
    }.get(tendency, "?")


def _tendency_label(tendency: PressureTendency) -> str:
    return {
        PressureTendency.RAPID_FALL: "Rapid fall",
        PressureTendency.SLOW_FALL: "Slow fall",
        PressureTendency.STABLE: "Stable",
        PressureTendency.RISING: "Rising",
    }.get(tendency, "Unknown")
