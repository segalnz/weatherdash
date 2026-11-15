"""Restart-safe regime hysteresis using the previous successful forecast snapshot.

Engineering defaults, not validated event probabilities. Sensor validity always wins.
"""
from datetime import datetime, timedelta
from forecast_state import RegimeMemory, RegimeTransition

VERSION = 'regime_v1'
ENTRY_SECONDS = 600
ENTRY_CYCLES = 3
EXIT_SECONDS = 600
EXIT_CYCLES = 2
MAX_GAP_SECONDS = 900
FIELDS = {'deteriorating': ('pressure',),
          'possible_frontal_passage': ('pressure', 'temperature', 'speed', 'direction'),
          'mist_potential': ('temperature', 'humidity', 'speed')}


def _valid_inputs(id, snapshot, features, now):
    from synthesis import fresh, finite
    if not all(fresh(snapshot, field, now) for field in FIELDS[id]):
        return False
    if id in ('deteriorating', 'possible_frontal_passage'):
        if not finite(snapshot.pressure_hpa) or not finite(features.pressure_delta_3h):
            return False
    if id == 'possible_frontal_passage':
        return (all(finite(v) for v in (snapshot.temperature_c, snapshot.wind_speed_kmh,
                    snapshot.wind_direction_deg, features.pressure_delta_1h,
                    features.temp_delta_1h, features.wind_dir_change_1h))
                and snapshot.wind_speed_kmh >= 8)
    if id == 'mist_potential':
        return (snapshot.humidity_quality == 'ok' and all(finite(v) for v in
                (snapshot.temperature_c, snapshot.humidity_pct, snapshot.dewpoint_c, snapshot.wind_speed_kmh)))
    return True


def _hold(id, snapshot, features):
    """Broader exit bands than entry bands; cannot revive missing/invalid inputs."""
    if id == 'deteriorating':
        return features.pressure_delta_3h <= -0.5
    if id == 'possible_frontal_passage':
        return (features.pressure_delta_3h >= .5 and features.pressure_delta_1h >= .2
                and features.temp_delta_1h <= -.5 and features.wind_dir_change_1h >= 30)
    return snapshot.temperature_c - snapshot.dewpoint_c <= 3 and snapshot.wind_speed_kmh <= 12


def track(candidates, snapshot, features, previous, now):
    prior = {}
    transitions = []
    if previous is not None and previous.tracking_version == VERSION:
        gap = (now - datetime.fromisoformat(previous.as_of)).total_seconds()
        if gap < 0:
            raise ValueError('Regime updates must not move backwards in time')
        if gap <= MAX_GAP_SECONDS:
            prior = previous.regime_memory
        elif previous.regime_memory:
            transitions.extend(RegimeTransition(id=id, state='withheld', reason='Forecast update gap exceeded fifteen minutes; persistence reset.') for id in previous.regime_memory)
    current = {c.id: c for c in candidates}
    memories, assessments = {}, []
    for id in sorted(set(current) | set(prior)):
        candidate, old = current.get(id), prior.get(id)
        if not _valid_inputs(id, snapshot, features, now):
            transitions.append(RegimeTransition(id=id, state='withheld', reason='Required observations or trend coverage are missing, stale or invalid.'))
            continue
        stamps = {field: snapshot.observation_times[field] for field in FIELDS[id]}
        if old and any(datetime.fromisoformat(stamps[k]) < datetime.fromisoformat(old.observation_times[k]) for k in stamps):
            transitions.append(RegimeTransition(id=id, state='withheld', reason='Observation time moved backwards; persistence reset.'))
            continue
        new_cycle = old is None or all(datetime.fromisoformat(stamps[k]) > datetime.fromisoformat(old.observation_times[k]) for k in stamps)
        if old and (now - datetime.fromisoformat(old.last_seen)).total_seconds() > MAX_GAP_SECONDS:
            old = None
        if candidate is None and (old is None or old.persistent_since is None):
            if old:
                transitions.append(RegimeTransition(id=id, state='ended', reason='Candidate did not sustain its entry conditions.'))
            continue
        if candidate is not None:
            memory = old.model_copy(deep=True) if old else RegimeMemory(assessment=candidate,
                first_seen=now.isoformat(), last_seen=now.isoformat(), last_qualifying=now.isoformat(), observation_times=stamps)
            memory.assessment = candidate.model_copy(deep=True)
            if new_cycle:
                memory.observation_times = stamps
                memory.last_seen = now.isoformat()
                memory.last_qualifying = now.isoformat()
                if old:
                    memory.fresh_cycles += 1
                memory.exit_started, memory.exit_cycles = None, 0
            if (memory.persistent_since is None and memory.fresh_cycles >= ENTRY_CYCLES
                    and (now-datetime.fromisoformat(memory.first_seen)).total_seconds() >= ENTRY_SECONDS):
                memory.persistent_since = now.isoformat()
                transitions.append(RegimeTransition(id=id, state='persistent', reason='Entry conditions persisted for ten minutes across at least three fresh observation cycles.'))
            memory.assessment.persistence = 'persistent' if memory.persistent_since else 'candidate'
        else:
            memory = old.model_copy(deep=True)
            # A held assessment refers to the previous hypothesis, not fresh support IDs.
            memory.assessment.evidence_ids = []
            memory.assessment.support = 'weakening'
            memory.assessment.persistence = 'weakening'
            memory.assessment.caveat = 'Earlier persistent pattern is weakening; its original evidence is not current support.'
            if new_cycle:
                memory.observation_times = stamps
                memory.last_seen = now.isoformat()
                if not _hold(id, snapshot, features):
                    memory.exit_started = memory.exit_started or now.isoformat()
                    memory.exit_cycles += 1
                else:
                    memory.exit_started, memory.exit_cycles = None, 0
            # Hysteresis is bounded: never retain an unrenewed hypothesis indefinitely.
            weak_age = (now-datetime.fromisoformat(memory.last_qualifying)).total_seconds()
            exit_age = (now-datetime.fromisoformat(memory.exit_started)).total_seconds() if memory.exit_started else 0
            if (weak_age >= MAX_GAP_SECONDS or
                    (exit_age >= EXIT_SECONDS and memory.exit_cycles >= EXIT_CYCLES)):
                transitions.append(RegimeTransition(id=id, state='ended', reason='Exit conditions persisted, or the weakening hypothesis reached its fifteen-minute limit.'))
                continue
        memory.assessment.first_seen = memory.first_seen
        memory.assessment.persistent_since = memory.persistent_since
        memory.assessment.fresh_cycles = memory.fresh_cycles
        memories[id] = memory
        assessments.append(memory.assessment)
    return assessments, memories, transitions
