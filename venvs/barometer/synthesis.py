"""Deterministic evidence synthesis; does not alter MET forecast values or timing."""
from datetime import datetime, timedelta, timezone
import math
from derived_features import dew_point
from forecast_state import EvidenceItem, RegimeAssessment, ForecastWindow, ForecastSynthesis


def finite(value):
    return value is not None and math.isfinite(value)


def fresh(snapshot, field, now):
    try:
        stamp = datetime.fromisoformat(snapshot.observation_times[field])
        return -30 <= (now - stamp).total_seconds() < 600
    except (KeyError, TypeError, ValueError):
        return False


def build_evidence(snapshot, features, now):
    items = []

    def add(id, group, message, value=None, unit=None, hours=None, supports=None,
            quality='ok', source='local-derived', observed_at=None):
        fields = {'pressure': ['pressure'], 'thermodynamics': ['temperature', 'humidity'],
                  'wind': ['speed', 'direction'], 'gauge': ['rain']}.get(group, [])
        times = {k: snapshot.observation_times[k] for k in fields if k in snapshot.observation_times}
        items.append(EvidenceItem(id=id, dependency_group=group, message=message,
                                  value=value, unit=unit, window_hours=hours,
                                  supports=supports or [], quality=quality,
                                  source=source, observed_at=observed_at, assessed_at=now.isoformat(), observation_times=times))

    pressure_ok = fresh(snapshot, 'pressure', now) and finite(snapshot.pressure_hpa)
    p1, p3 = features.pressure_delta_1h, features.pressure_delta_3h
    if not pressure_ok or not finite(p3):
        add('pressure_unavailable', 'pressure', 'Pressure trend unavailable: stale readings or insufficient reliable coverage.', quality='unknown')
    else:
        if p3 <= -1:
            add('pressure_falling', 'pressure', f'Pressure fell {abs(p3):.1f} hPa over three hours; supports deterioration, not rain timing.', p3, 'hPa', 3, ['deteriorating'])
            if finite(p1) and p1 < p3 / 3 - 0.5:
                add('pressure_accelerating', 'pressure', 'The recent pressure fall is accelerating; this is part of the same pressure evidence.', p1, 'hPa', 1, ['deteriorating'])
        elif p3 >= 1:
            add('pressure_rising', 'pressure', f'Pressure rose {p3:.1f} hPa over three hours; showers may still persist.', p3, 'hPa', 3, ['pressure_recovery'])
        else:
            add('pressure_steady', 'pressure', 'Three-hour pressure trend is relatively steady.', p3, 'hPa', 3)

    humidity_ok = (snapshot.humidity_quality == 'ok' and fresh(snapshot, 'humidity', now)
                   and fresh(snapshot, 'temperature', now)
                   and finite(snapshot.temperature_c) and finite(snapshot.humidity_pct))
    moist = features.dewpoint_delta_1h
    if humidity_ok:
        snapshot.dewpoint_c = dew_point(snapshot.temperature_c, snapshot.humidity_pct)
        if finite(moist) and moist >= 0.5:
            add('moisture_increasing', 'thermodynamics', 'Dew point is rising: moisture increase supports deterioration.', moist, 'C', 1, ['deteriorating'])
        h, t = features.humidity_delta_1h, features.temp_delta_1h
        if finite(h) and finite(t) and finite(moist) and h >= 3 and t < 0 and abs(moist) < 0.5:
            add('humidity_from_cooling', 'thermodynamics', 'Relative humidity is rising with cooling and little dew-point change; not independent rain evidence.', h, '%', 1)
    else:
        snapshot.dewpoint_c = None
        add('moisture_unavailable', 'thermodynamics', 'Moisture interpretation withheld: temperature/humidity stale, missing or quality flagged.', quality='unknown')

    if fresh(snapshot, 'temperature', now) and finite(features.temp_delta_1h) and features.temp_delta_1h <= -1:
        add('temperature_cooling', 'thermodynamics', 'Temperature fell over the last hour; day/night and air-mass explanations remain possible.', features.temp_delta_1h, 'C', 1, ['transition'])

    wind_ok = (fresh(snapshot, 'speed', now) and finite(snapshot.wind_speed_kmh))
    if wind_ok and snapshot.wind_speed_kmh > 25:
        add('windy_now', 'wind', 'Local observations currently indicate windy conditions.', snapshot.wind_speed_kmh, 'km/h', source='local-wind')
    if wind_ok and finite(features.wind_speed_delta_1h) and features.wind_speed_delta_1h >= 5:
        add('wind_increasing', 'wind', 'Wind speed increased over the last hour; local exposure may differ from the model grid.', features.wind_speed_delta_1h, 'm/s', 1)
    # Calm or missing wind must not confirm a front from direction changes alone.
    shift_ok = (wind_ok and snapshot.wind_speed_kmh >= 8 and fresh(snapshot, 'direction', now)
                and finite(features.wind_dir_change_1h))
    if shift_ok and features.wind_dir_change_1h >= 45:
        add('wind_shift', 'wind', 'A sustained-window wind-direction change supports a possible transition.', features.wind_dir_change_1h, 'degrees', 1, ['transition'])
    if humidity_ok and snapshot.dewpoint_c is not None and wind_ok:
        spread = snapshot.temperature_c - snapshot.dewpoint_c
        if spread <= 2 and snapshot.wind_speed_kmh <= 8:
            add('mist_potential', 'thermodynamics', 'Small temperature/dew-point spread and light wind support mist or low-cloud potential; fog is not confirmed.', spread, 'C', supports=['mist_potential'])

    if snapshot.rain_quality == 'ok' and finite(snapshot.rain_recent_mm):
        wet = snapshot.rain_recent_mm > 0
        add('recent_rain' if wet else 'recent_dry', 'gauge',
            'Recent complete gauge interval recorded rain.' if wet else 'Recent complete gauge interval was dry; this does not establish the next hour.',
            snapshot.rain_recent_mm, 'mm', source='local-gauge')
    else:
        add('rain_unavailable', 'gauge', 'Recent gauge rainfall unavailable; missing is not dry.', quality='unknown')
    return items


def model_windows(raw_metno, now):
    """Intersect full MET hour classifications with the six-hour outlook.

    Do not prorate rain amounts, bridge missing hours or claim precise rain onset.
    """
    horizon = now + timedelta(hours=6)
    windows = []
    for row in sorted(raw_metno.values(), key=lambda r: r.get('valid_time', '')):
        try:
            start = datetime.fromisoformat(row['valid_time'])
            end = start + timedelta(hours=1)
            if end <= now or start >= horizon:
                continue
            amount = row.get('precipitation')
            if row.get('precipitation_period_hours') != 1 or not finite(amount) or amount < 0:
                continue
            start, end = max(start, now), min(end, horizon)
        except (KeyError, ValueError, TypeError):
            continue
        state = 'wet' if amount > 0 else 'dry'
        if windows and windows[-1].state == state and windows[-1].end == start.isoformat():
            windows[-1].end = end.isoformat()
        else:
            windows.append(ForecastWindow(start=start.isoformat(), end=end.isoformat(), state=state))
    return windows


def synthesise(snapshot, features, raw_metno, now=None, previous=None):
    now = now or datetime.now(timezone.utc)
    ledger = build_evidence(snapshot, features, now)
    by_id = {e.id: e for e in ledger}
    regimes = []
    falling = 'pressure_falling' in by_id
    moistening = 'moisture_increasing' in by_id
    if falling:
        ids = ['pressure_falling'] + (['pressure_accelerating'] if 'pressure_accelerating' in by_id else [])
        if moistening:
            ids.append('moisture_increasing')
        groups = sorted({by_id[id].dependency_group for id in ids})
        regimes.append(RegimeAssessment(id='deteriorating', support='corroborated' if len(groups) >= 2 else 'limited', evidence_ids=ids, dependency_groups=groups))
    if ('pressure_rising' in by_id and finite(features.pressure_delta_1h)
            and features.pressure_delta_1h >= 0.5 and 'wind_shift' in by_id
            and fresh(snapshot, 'temperature', now) and finite(features.temp_delta_1h)
            and features.temp_delta_1h <= -1):
        regimes.append(RegimeAssessment(id='possible_frontal_passage', support='possible',
            evidence_ids=['pressure_rising', 'wind_shift', 'temperature_cooling'], dependency_groups=['pressure', 'wind', 'thermodynamics'],
            caveat='Cooling also contributes; a front is not confirmed and post-front showers remain possible.'))
    if 'mist_potential' in by_id:
        regimes.append(RegimeAssessment(id='mist_potential', support='possible', evidence_ids=['mist_potential'], dependency_groups=['thermodynamics'], caveat='No visibility or cloud-base observation confirms fog.'))

    from regime_tracking import track, VERSION
    regimes, memory, transitions = track(regimes, snapshot, features, previous, now)

    windows = model_windows(raw_metno, now)
    wet = any(w.state == 'wet' for w in windows)
    covered_seconds = sum((datetime.fromisoformat(w.end)-datetime.fromisoformat(w.start)).total_seconds() for w in windows)
    complete = covered_seconds >= 6 * 3600
    limitations = [e.message for e in ledger if e.quality != 'ok']
    limitations.extend(t.reason for t in transitions if t.state == 'withheld')
    if not complete:
        limitations.append('MET hourly precipitation does not cover the full next six hours; missing periods remain unknown.')
    # Compare patterns, not observed dry intervals against future wet intervals.
    agreement = 'not_assessed'
    conflicts = []
    if falling and wet:
        agreement = 'consistent_signals'
    elif falling and complete and not wet:
        agreement = 'mixed_signals'
        conflicts.append('Local pressure suggests deterioration while MET forecasts dry hours; falling pressure alone does not establish rain.')
    summary = []
    if 'windy_now' in by_id:
        summary.append('It is currently windy at the station; persistence through all six hours is not assumed.')
    for regime in regimes:
        if regime.persistence == 'weakening':
            summary.append({'deteriorating': 'The earlier deterioration signal is weakening.',
                            'possible_frontal_passage': 'The earlier possible frontal-passage pattern is weakening.',
                            'mist_potential': 'The earlier mist/low-cloud signal is weakening.'}[regime.id])
        elif regime.id == 'deteriorating':
            qualifier = 'Persistent local evidence' if regime.persistence == 'persistent' else 'An emerging local pattern'
            summary.append(qualifier + ' supports possible deterioration; rain timing still comes from MET.')
        elif regime.id == 'possible_frontal_passage':
            qualifier = 'A persistent pattern' if regime.persistence == 'persistent' else 'An emerging pattern'
            summary.append(qualifier + ' suggests possible recent frontal passage; a front is not confirmed and showers may persist.')
        elif regime.id == 'mist_potential':
            qualifier = 'Persistent local conditions' if regime.persistence == 'persistent' else 'Emerging local conditions'
            summary.append(qualifier + ' permit mist or low cloud; fog is unconfirmed.')
    if wet:
        summary.append('MET forecasts rain in the indicated hourly windows; local sensors do not independently establish arrival time.')
    elif complete:
        summary.append('MET forecasts no rain over the next six hours.')
    elif windows:
        summary.append('Available MET hours are dry, but the six-hour forecast is incomplete.')
    else:
        summary.append('MET hourly rain guidance is unavailable; the next six hours cannot be classified as wet or dry.')
    if wet and not complete:
        summary.append('Guidance is incomplete outside the available hourly windows.')
    summary.extend(conflicts)
    if not regimes:
        summary.append('Local evidence does not establish a changing weather regime.')
    expiry = now + timedelta(minutes=10)
    for field in ('pressure', 'temperature', 'humidity', 'speed', 'direction'):
        if fresh(snapshot, field, now):
            expiry = min(expiry, datetime.fromisoformat(snapshot.observation_times[field]) + timedelta(minutes=10))
    return ForecastSynthesis(as_of=now.isoformat(), valid_until=expiry.isoformat(), summary=' '.join(summary),
        evidence=ledger, regimes=regimes, windows=windows, model_local_agreement=agreement,
        conflicts=conflicts, limitations=list(dict.fromkeys(limitations)),
        tracking_version=VERSION, regime_memory=memory, regime_transitions=transitions,
        uncertainty='Qualitative support, not calibrated probability. Persistent means repeated local evidence, not a confirmed meteorological event.')
