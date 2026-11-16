import math
from html import escape
import textwrap

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


def _basis_text(strip):
    synthesis = strip.synthesis
    if synthesis.strip_explanation:
        return synthesis.strip_explanation
    # Format existing facts only; this is not another forecast interpretation.
    facts, groups = [], set()
    for item in synthesis.evidence:
        if item.quality != 'ok' or item.dependency_group in groups:
            continue
        value = item.value
        labels = {
            'pressure_falling': f'pressure {value:+.1f} hPa/3h' if value is not None else 'falling pressure',
            'pressure_rising': f'pressure {value:+.1f} hPa/3h' if value is not None else 'rising pressure',
            'pressure_steady': f'pressure {value:+.1f} hPa/3h' if value is not None else 'steady pressure',
            'moisture_increasing': 'dew point rising',
            'humidity_from_cooling': 'humidity rise consistent with cooling',
            'temperature_cooling': 'temperature falling',
            'windy_now': f'wind {value:.0f} km/h' if value is not None else 'windy locally',
            'wind_increasing': 'wind increasing',
            'wind_shift': 'wind direction changing',
            'mist_potential': 'small dew-point spread and light wind',
            'recent_rain': f'recent gauge rain {value:.1f} mm' if value is not None else 'recent gauge rain',
            'recent_dry': 'recent gauge interval dry',
        }
        if item.id in labels:
            facts.append(labels[item.id])
            groups.add(item.dependency_group)
    regional = 'MET Norway hourly guidance' if synthesis.windows else 'MET hourly rain guidance unavailable'
    return regional + ('; local evidence: ' + '; '.join(facts) if facts else '; reliable local diagnostics limited')


def _strip_notes(strip, width):
    """At most five visible lines, with complete reasoning in the SVG description."""
    capacity = max(12, int(width / (13 * .65)))
    if strip.synthesis is None:
        message = 'Forecast explanation awaiting a fresh outlook update.'
        return textwrap.wrap(message, width=capacity, max_lines=2, placeholder='…'), message
    synthesis = strip.synthesis
    basis = _basis_text(strip)
    notes = textwrap.wrap('Outlook: ' + ' '.join(synthesis.summary.split()), width=capacity, max_lines=2, placeholder='…')
    notes += textwrap.wrap('Basis: ' + ' '.join(basis.split()), width=capacity, max_lines=2, placeholder='…')
    if synthesis.engine == 'llm-runtime':
        # Validated interpreter text must fit in full; never silently truncate it.
        blocks = [('Outlook: '+synthesis.summary, 2), ('Basis: '+basis, 2), ('Note: '+synthesis.uncertainty, 1)]
        complete=[]
        for text,budget in blocks:
            wrapped=textwrap.wrap(' '.join(text.split()),width=capacity)
            if len(wrapped)>budget:
                message='Interpreter explanation needs shortening; awaiting a corrected kiosk update.'
                return textwrap.wrap(message,width=capacity), '\n'.join(text for text,_ in blocks)
            complete.extend(wrapped)
        full='\n'.join([synthesis.summary,'Basis: '+basis,synthesis.uncertainty,*synthesis.conflicts,*synthesis.limitations])
        return complete, full
    cautions = synthesis.conflicts + synthesis.limitations
    if cautions:
        notice = 'Note: ' + cautions[0]
        if len(cautions) > 1:
            notice += f' ({len(cautions)-1} further limitations in the full outlook.)'
        notes += textwrap.wrap(' '.join(notice.split()), width=capacity, max_lines=1, placeholder='…')
    full = '\n'.join([synthesis.summary, 'Basis: ' + basis, *cautions, synthesis.uncertainty,
                      'Detailed evidence:', *(item.message for item in synthesis.evidence)])
    return notes, full


def render_forecast_strip(strip: ForecastStrip) -> str:
    cols = len(strip.slots)
    total_w = cols * SLOT_W + MARGIN * 2
    notes, full_explanation = _strip_notes(strip, total_w - 2 * MARGIN)
    footer_h = max(FOOTER_H, 28 + len(notes) * 18)
    total_h = OFFSET_Y + SLOT_H + footer_h

    lines = [f'<svg xmlns="http://www.w3.org/2000/svg"'
             f' width="{total_w}" height="{total_h}"'
             f' viewBox="0 0 {total_w} {total_h}"'
             f' style="max-width:100%;height:auto" role="img" aria-labelledby="strip-title strip-description">']

    lines.append('<title id="strip-title">Six-hour forecast — ' + escape(strip.generated_at) + '</title>')
    lines.append('<desc id="strip-description">' + escape(full_explanation) + '</desc>')
    lines.append(f'<rect width="100%" height="100%" fill="{BG}" rx="14"/>')
    lines.append(_defs_block())

    _draw_pressure_graph(lines, strip)

    for i, slot in enumerate(strip.slots):
        x = MARGIN + i * SLOT_W
        _draw_slot(lines, x, OFFSET_Y, slot, i)

    note_y = OFFSET_Y + SLOT_H + 20
    for index, note in enumerate(notes):
        lines.append(f'<text data-role="forecast-explanation" x="{MARGIN}" y="{note_y + index * 18}" fill="{TEXT_SECONDARY}" font-family="monospace" font-size="13">{escape(note)}</text>')
    metadata = f'Moonshine Valley Barometer — {strip.generated_at} | evidence score, not rain probability'
    if strip.interpreter_status:
        metadata = f'LLM {strip.interpreter_status} — {strip.interpreter_model} | {strip.generated_at} | {strip.interpreter_attempts} attempt(s)'

    capacity = max(12, int((total_w - 2 * MARGIN) / (13 * .65)))
    metadata = textwrap.shorten(metadata, width=capacity, placeholder='…')
    lines.append(f'<text x="{MARGIN}" y="{total_h - 8}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="13">{escape(metadata)}</text>')
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


def _pressure_outlook(strip):
    slots=strip.slots
    llm=any(getattr(slot, 'source', '') == 'llm-runtime' for slot in slots[1:])
    reference=not llm and any(getattr(slot, 'source', '').startswith('metno') for slot in slots[1:])
    if not slots:
        return [], 'unavailable'
    if llm:
        now=strip.rooftop.pressure_hpa if strip.rooftop else None
        return [now]+[getattr(slot,'forecast_pressure_hpa',None) for slot in slots[1:]], 'llm-runtime'
    if reference:
        # Do not splice a local gauge with a different pressure reduction/calibration.
        return [getattr(slot,'baseline_pressure_hpa',None) for slot in slots], 'metno-reference'
    return [None]*len(slots), 'unavailable'


def _draw_pressure_graph(lines: list, strip: ForecastStrip):
    """Future pressure aligned with the future slots, never historical sensor bins."""
    values,source=_pressure_outlook(strip)
    valid=lambda value: value is not None and math.isfinite(value) and value>0
    future=[value for value in values[1:] if valid(value)]
    if future and source=='llm-runtime':
        observed=f'{values[0]:.1f} hPa' if valid(values[0]) else 'unavailable'
        caption=f'Pressure outlook — local hPa, LLM forecast | observed Now: {observed}; tendency: {_tendency_label(strip.pressure_tendency)}'
    elif future and source=='metno-reference':
        caption='Pressure outlook — MET sea-level reference, next 6h | local observations are not spliced into this reference'
    else:
        caption='Pressure outlook unavailable — historical observations are not future forecast points'
    lines.append(f'<text x="{MARGIN}" y="28" fill="{TEXT_SECONDARY}" font-family="sans-serif" font-size="14" font-weight="bold">{escape(caption)}</text>')
    if not future:
        lines.append(f'<text x="{MARGIN + 20}" y="{GRAPH_Y + 50}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="13">Awaiting a validated pressure outlook; no curve invented or filled.</text>')
        return
    finite_values=[value for value in values if valid(value)]
    low,high=min(finite_values),max(finite_values)
    pad=max((high-low)*.3,.3)
    floor=math.floor(low-pad);ceiling=math.ceil(high+pad)
    span=ceiling-floor
    gx=MARGIN;gy=GRAPH_Y;gw=len(strip.slots)*SLOT_W-10
    lines.append(f'<rect x="{gx}" y="{gy}" width="{gw}" height="{GRAPH_H}" rx="6" fill="{GRAPH_BG}"/>')
    for i in range(4):
        y=gy+GRAPH_H-GRAPH_H*i/3
        value=floor+span*i/3
        lines.append(f'<line x1="{gx+40}" y1="{y:.1f}" x2="{gx+gw}" y2="{y:.1f}" stroke="{GRID_COLOR}"/>')
        lines.append(f'<text x="{gx+35}" y="{y+4:.1f}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="10" text-anchor="end">{value:.1f}</text>')
    x=lambda index:MARGIN+index*SLOT_W+(SLOT_W-10)/2
    y=lambda value:gy+GRAPH_H-GRAPH_H*(value-floor)/span
    segment=[]
    for i,value in enumerate(values):
        if valid(value):
            segment.append(f'{x(i):.1f},{y(value):.1f}')
        else:
            if len(segment)>=2:
                lines.append(f'<polyline data-role="pressure-outlook" points="{" ".join(segment)}" fill="none" stroke="{LINE_COLOR}" stroke-width="2"/>')
            segment=[]
    if len(segment)>=2:
        lines.append(f'<polyline data-role="pressure-outlook" points="{" ".join(segment)}" fill="none" stroke="{LINE_COLOR}" stroke-width="2"/>')
    for i,(slot,value) in enumerate(zip(strip.slots,values)):
        label='Now' if i==0 else getattr(slot,'time_label','')
        lines.append(f'<text x="{x(i):.1f}" y="{gy+GRAPH_H+12}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="10" text-anchor="middle">{escape(label)}</text>')
        if valid(value):
            point_source='observed' if i==0 and source=='llm-runtime' else source
            color=NOW_COLOR if point_source=='observed' else LINE_COLOR
            lines.append(f'<circle data-role="pressure-forecast-point" data-source="{point_source}" data-value="{value:.3f}" cx="{x(i):.1f}" cy="{y(value):.1f}" r="3" fill="{color}"/>')
        else:
            lines.append(f'<text data-role="pressure-unavailable" x="{x(i):.1f}" y="{gy+25}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="10" text-anchor="middle">Unavailable</text>')


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
    if slot.confidence_kind == 'llm_qualitative' and slot.confidence_level:
        lines.append(f'<text x="{cx}" y="{conf_y + 9}" fill="{TEXT_SECONDARY}" font-family="sans-serif" font-size="11" text-anchor="middle">Support: {escape(slot.confidence_level)}</text>')
    elif slot.confidence_kind == 'unavailable':
        lines.append(f'<text x="{cx}" y="{conf_y + 9}" fill="{TEXT_MUTED}" font-family="sans-serif" font-size="11" text-anchor="middle">Unavailable</text>')
    else:
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
