"""Runtime weather interpretation with explicit challenges and bounded revision.

The deterministic forecast is advisory input, never a substituted LLM result.
"""
from datetime import datetime, timezone, timedelta
import hashlib
import copy
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import time
import textwrap
from typing import Literal
import urllib.error
import urllib.request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from forecast_state import AtmosphericState, ForecastStrip
from synthesis import fresh


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Claim(StrictModel):
    reference: str = Field(min_length=3, max_length=150)
    value: float | str | bool | None


class AdvisoryResponse(StrictModel):
    challenge_id: str
    action: Literal['revised', 'justified']
    explanation: str = Field(min_length=20, max_length=400)
    evidence_references: list[str] = Field(min_length=1, max_length=8)


class InterpretedHour(StrictModel):
    hour_offset: int = Field(ge=1, le=6, strict=True)
    valid_time: str
    state: AtmosphericState
    label: str = Field(min_length=1, max_length=32)
    temperature_c: float | None = Field(ge=-60, le=65, strict=True)
    wind_speed_kmh: float | None = Field(ge=0, le=250, strict=True)
    wind_direction_deg: float | None = Field(ge=0, le=360, strict=True)
    precipitation_mm: float | None = Field(ge=0, le=200, strict=True)
    confidence: Literal['low', 'medium', 'high']
    rationale: str = Field(min_length=10, max_length=240)
    evidence_references: list[str] = Field(default_factory=list, max_length=8)


class Interpretation(StrictModel):
    input_id: str
    summary: str = Field(min_length=10, max_length=240)
    strip_explanation: str = Field(min_length=10, max_length=220)
    uncertainty: str = Field(min_length=10, max_length=120)
    model_local_agreement: Literal['consistent', 'mixed', 'insufficient']
    alternative: str = Field(min_length=10, max_length=240)
    hours: list[InterpretedHour] = Field(min_length=6, max_length=6)
    claims: list[Claim] = Field(min_length=1, max_length=24)
    acknowledged_limitations: list[str] = Field(max_length=24)
    advisory_responses: list[AdvisoryResponse] = Field(default_factory=list, max_length=24)


SYSTEM = '''You are the primary interpreter of a local six-hour weather forecast in rural New Zealand.
Interpret the recent observations/history together with MET Norway, as a careful experienced observer would.
The deterministic diagnostics/regimes are advisory hypotheses, not a forecast you must reproduce.
Return JSON only, matching the supplied response_schema. Do not return private chain-of-thought;
provide concise evidence-based conclusions, rationale, uncertainty and an important alternative.
Use exact input_id and the six supplied hour offsets/valid times. Stay within outlook_window; do not extend the narrative into the next morning/night beyond that window. Units: C, hPa, km/h, degrees, mm per model hour.
Model precipitation applies to the hour STARTING at its valid_time. Gauge rain is a past interval, not an instantaneous rain detector.
Missing values are unknown, never zero. Null is permitted in uncertain predictions. Preserve genuine zero values.
Do not invent observations, visibility/radar evidence, sensor health or probabilities. Any numeric observation stated must have a matching claim.
claims reference exact paths in observations, trends, history or met_forecast (e.g. observations.temperature_c, trends.pressure_delta_3h,
met_forecast.2.temperature_c, history.pressure_hpa.points.0.value). Claim values must match those inputs.
met_forecast is a DICTIONARY keyed by hour offset "1" through "6", NOT a zero-indexed list.
Use paths/values from evidence_catalog for exact claims; do not cite null values as supporting evidence.
Set advisory_responses=[] initially; only answer challenge_ids actually sent by Weatherdash, never missing-data flags.
Use all required limitation IDs in acknowledged_limitations and reflect material limitations in uncertainty.
Hourly evidence_references must cite existing non-null paths, not deterministic narrative strings.
You may disagree with MET or advisory rules, but explain departures; pressure/humidity alone do not establish precise rain arrival or amounts.
Use state unknown with null predictions where evidence is insufficient. Avoid probability percentages.
KIOSK: aim for summary <=180 characters, strip_explanation <=160, uncertainty <=90. Hard maxima are 240/220/120 respectively. Plain English text, ordinary punctuation, no emoji, no markdown, no newlines.
strip_explanation must identify the key evidence behind your forecast, not just repeat the outlook. Fit the limits; never rely on scrolling.
If challenged, revise factual/schema problems. For advisory challenges you may revise OR justify disagreement using references.
Do not merely repeat a disputed answer. Justified advisory disagreements remain visible; hard factual constraints cannot be waived.
'''


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 5) if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    try:
        numeric = float(value)
        return round(numeric,5) if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def series(frame, field, unit, minutes, scale=1):
    import pandas as pd
    points=[]
    if not frame.empty and field in frame:
        for stamp,value in frame[['_time',field]].itertuples(index=False,name=None):
            if pd.isna(stamp):
                continue
            points.append({'time':stamp.isoformat(),'value':None if pd.isna(value) else clean(float(value)*scale)})
    return dict(unit=unit, interval_minutes=minutes, oldest_first=True, points=points,
                data_quality='partial' if not points or any(p['value'] is None for p in points) else 'available',
                gaps='Missing intervals are unknown; never forward-fill.')


def make_payload(strip, roof_history, rain_history, pressure_history):
    now=datetime.fromisoformat(strip.generated_at_iso)
    snapshot=strip.rooftop
    observations=snapshot.model_dump(mode='json')
    limitations=[{'id':'history_coverage','reason':'History aggregates contain available numeric samples; per-window coverage can be partial. Respect timestamps and gaps.'}]
    mapping={'temperature_c':'temperature','humidity_pct':'humidity','pressure_hpa':'pressure',
             'wind_speed_kmh':'speed','wind_direction_deg':'direction','wind_gust_kmh':'gust'}
    for field,sensor in mapping.items():
        if observations[field] is None or not fresh(snapshot,sensor,now):
            observations[field]=None
            limitations.append({'id':'missing_'+field,'reason':field+' unavailable or stale; do not claim a current observation.'})
    if not fresh(snapshot,'rain',now):
        observations['rain_recent_mm']=None
        observations['rain_detected']=None
        observations['rain_quality']='unknown'
        limitations.append({'id':'rain_quality','reason':'No recent complete gauge interval; missing is not dry and is not instantaneous rain.'})
    if snapshot.humidity_quality!='ok':
        limitations.append({'id':'humidity_quality','reason':snapshot.humidity_quality_reason or 'Ambient humidity is not fully reliable.'})
    trends=strip.derived.model_dump(mode='json')
    if observations['pressure_hpa'] is None:
        trends['pressure_delta_1h']=trends['pressure_delta_3h']=None
    if snapshot.humidity_quality!='ok' or observations['humidity_pct'] is None or observations['temperature_c'] is None:
        trends['dewpoint_delta_1h']=trends['humidity_delta_1h']=None
        observations['dewpoint_c']=None
    if observations['temperature_c'] is None:trends['temp_delta_1h']=None
    if observations['wind_speed_kmh'] is None:trends['wind_speed_delta_1h']=None
    if observations['wind_speed_kmh'] is None or observations['wind_direction_deg'] is None:trends['wind_dir_change_1h']=None
    if trends.get('wind_speed_delta_1h') is not None:
        trends['wind_speed_delta_1h']*=3.6
    for field in ['pressure_delta_3h','dewpoint_delta_1h','wind_dir_change_1h']:
        if trends.get(field) is None:
            limitations.append({'id':'missing_'+field,'reason':field+' lacks reliable trend coverage.'})
    regional=[]
    for slot in strip.slots[1:]:
        row=dict(hour_offset=slot.hour_offset,valid_time=slot.forecast_valid_time,
                 temperature_c=slot.baseline_temperature_c,wind_speed_kmh=slot.baseline_wind_speed_kmh,
                 wind_direction_deg=slot.baseline_wind_dir_deg,precipitation_mm=slot.baseline_precipitation_mm,
                 pressure_hpa=slot.forecast_pressure_hpa,humidity_pct=slot.forecast_humidity_pct,
                 symbol_code=slot.met_symbol_code,forecast_issued=slot.forecast_issued,
                 forecast_retrieved_at=slot.forecast_retrieved_at,precipitation_period_hours=1 if slot.baseline_precipitation_mm is not None else None)
        if row['valid_time'] is None:
            row['valid_time']=(now.replace(minute=0,second=0,microsecond=0)+timedelta(hours=slot.hour_offset)).isoformat()
        if row['precipitation_mm'] is None:
            limitations.append({'id':f'missing_met_rain_{slot.hour_offset}','reason':'Hourly MET rain is unknown for this slot; do not treat it as dry.'})
        regional.append(row)
    history={
        'pressure_hpa':series(pressure_history,'pressure','hPa',5),
        'temperature_c':series(roof_history,'temperature','C',5),
        'humidity_pct':series(roof_history,'humidity','%',5),
        'wind_speed_kmh':series(roof_history,'speed','km/h',5,3.6),
        'wind_direction_deg':series(roof_history,'direction','degrees',5),
        'gust_kmh':series(roof_history,'gust','km/h',5,3.6),
        'rainfall_mm':series(rain_history,'rainfall_mm','mm per completed 10-minute interval',10)}
    from zoneinfo import ZoneInfo
    payload=clean(dict(schema_version=1,prompt_version='runtime_v1',as_of=strip.generated_at_iso,
        local_time=now.astimezone(ZoneInfo('Pacific/Auckland')).isoformat(),
        outlook_window={'start_local':now.astimezone(ZoneInfo('Pacific/Auckland')).isoformat(),'end_local':(now+timedelta(hours=6)).astimezone(ZoneInfo('Pacific/Auckland')).isoformat()},
        trend_units={'pressure_delta_1h':'hPa / 1h','pressure_delta_3h':'hPa / 3h','temp_delta_1h':'C / 1h','dewpoint_delta_1h':'C / 1h','humidity_delta_1h':'percentage-point / 1h','wind_speed_delta_1h':'km/h / 1h','wind_dir_change_1h':'degrees','rain_persistence_1h':'fraction of observed recent gauge intervals'},
        kiosk_contract={'characters_per_line':140,'summary_lines':2,'basis_lines':2,'uncertainty_lines':1,'scrolling':False},
        time_contract='Current slot is observed conditions. Future targets are the next six clock-hour times; rain amounts describe their following hourly intervals, not precise onset.',
        location={'description':os.getenv('WEATHER_LOCATION','Rural Wellington region, New Zealand'),
                  'elevation_m':float(os.getenv('WEATHER_ALTITUDE_M','243')),'terrain':'hills'},
        observations=observations,trends=trends,history=history,met_forecast={str(r['hour_offset']):r for r in regional},
        lux_expected_current=None,lux_calibration='unavailable; do not infer cloud from raw lux without solar context',
        advisory_diagnostics=[e.model_dump(mode='json') for e in strip.synthesis.evidence],
        advisory_regimes=[r.model_dump(mode='json') for r in strip.synthesis.regimes],
        limitations=limitations,
        pressure_basis='Already corrected hPa; do not apply altitude/temperature/calibration correction again.'))
    catalog={}
    for root in ('observations','trends'):
        for key,value in payload[root].items():
            if type(value) in (int,float):catalog[root+'.'+key]=value
    for hour,row in payload['met_forecast'].items():
        for key,value in row.items():
            if type(value) in (int,float) or key=='symbol_code' and value is not None:
                catalog['met_forecast.'+hour+'.'+key]=value
    for item in payload['advisory_diagnostics']:
        if item.get('unit')=='m/s' and item.get('value') is not None:
            item['value']=clean(item['value']*3.6);item['unit']='km/h'
    payload['evidence_catalog']=catalog
    payload['input_id']=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return payload


def resolve(payload, reference):
    parts=reference.split('.')
    if parts[0] not in ('observations','trends','history','met_forecast'):
        raise ValueError('Unsupported evidence root')
    value=payload
    try:
        for part in parts:
            value=value[int(part)] if isinstance(value,list) else value[part]
    except (KeyError,IndexError,ValueError,TypeError):
        raise ValueError('Unknown evidence reference') from None
    if isinstance(value,(list,dict)):
        raise ValueError('Reference must identify a specific input value')
    return value


def validate(raw,payload,challenged):
    hard=[];advisory=[]
    try:
        parsed=Interpretation.model_validate(raw)
    except ValidationError as exc:
        errors=exc.errors(include_input=False,include_url=False)
        # Collect semantic issues too when only a bounded narrative field is long.
        # Placeholders are validation-only: any such error still blocks publication.
        narrative={'summary','strip_explanation','uncertainty','alternative'}
        salvage=isinstance(raw,dict) and all(len(e['loc'])==1 and e['loc'][0] in narrative and e['type']=='string_too_long' for e in errors)
        for error in errors:
            location='.'.join(map(str,error['loc']))
            message=error['msg']
            if location in narrative and isinstance(raw.get(location),str):
                message+=f"; received {len(raw[location])} characters. Rewrite more briefly with headroom."
            hard.append({'id':'schema:'+location,'message':message})
        if not salvage:
            return None,hard,[]
        draft=copy.deepcopy(raw)
        for error in errors:draft[error['loc'][0]]='Invalid text awaiting model correction.'
        try:parsed=Interpretation.model_validate(draft)
        except ValidationError:return None,hard,[]
    if parsed.input_id!=payload['input_id']:
        hard.append({'id':'input_id','message':'Response belongs to a different input; echo this exact input_id.'})
    expected={r['hour_offset']:r for r in payload['met_forecast'].values()}
    if sorted(h.hour_offset for h in parsed.hours)!=list(range(1,7)):
        hard.append({'id':'hour_offsets','message':'Provide exactly offsets 1 through 6, once each.'})
    refs=set()
    for h in parsed.hours:
        try:
            if datetime.fromisoformat(h.valid_time)!=datetime.fromisoformat(expected[h.hour_offset]['valid_time']):
                raise ValueError()
        except (ValueError,KeyError):
            hard.append({'id':f'h{h.hour_offset}.valid_time','message':'Use the exact supplied valid time.'})
        refs.update(h.evidence_references)
    for response in parsed.advisory_responses:
        refs.update(response.evidence_references)
        if response.challenge_id not in challenged:
            hard.append({'id':'unknown_challenge:'+response.challenge_id,'message':'Only answer advisory IDs actually challenged in the previous validation feedback.'})
    for ref in refs:
        try:
            if resolve(payload,ref) is None:
                raise ValueError('Missing values cannot support a forecast')
        except ValueError as exc:
            hard.append({'id':'reference:'+ref,'message':str(exc)})
    def substantive(ref):
        try:
            value=resolve(payload,ref)
            return (type(value) in (float,int) and math.isfinite(value)) or (ref.endswith('.symbol_code') and isinstance(value,str) and bool(value))
        except ValueError:
            return False
    for h in parsed.hours:
        has_prediction=h.state!=AtmosphericState.UNKNOWN or any(v is not None for v in (h.temperature_c,h.wind_speed_kmh,h.wind_direction_deg,h.precipitation_mm))
        if has_prediction and not any(substantive(r) for r in h.evidence_references):
            hard.append({'id':f'h{h.hour_offset}.support','message':'A prediction needs numeric observation/trend/model evidence or a MET weather symbol, not timestamps or quality labels alone.'})
    for response in parsed.advisory_responses:
        if response.action=='justified' and not any(substantive(r) for r in response.evidence_references):
            hard.append({'id':'justification:'+response.challenge_id,'message':'Justification must reference substantive input evidence.'})
    for claim in parsed.claims:
        try:
            actual=resolve(payload,claim.reference)
            if type(actual) in (float,int) and not isinstance(actual,bool):
                equal=type(claim.value) in (float,int) and abs(actual-claim.value)<=.011
            else:
                equal=type(claim.value) is type(actual) and claim.value==actual
            if not equal:
                raise ValueError('Claim does not match the supplied value')
        except ValueError as exc:
            hard.append({'id':'claim:'+claim.reference,'message':str(exc)})
    missing={l['id'] for l in payload['limitations']}-set(parsed.acknowledged_limitations)
    if missing:
        hard.append({'id':'limitations','message':'Acknowledge these actual limitations: '+', '.join(sorted(missing))})
    for name,text in [('summary',parsed.summary),('strip_explanation',parsed.strip_explanation),('uncertainty',parsed.uncertainty)]:
        prefix={'summary':'Outlook: ','strip_explanation':'Basis: ','uncertainty':'Note: '}[name]
        if len(textwrap.wrap(prefix+' '.join(text.split()),width=140)) > (1 if name=='uncertainty' else 2) or len(text.strip())<10:
            hard.append({'id':'kiosk_fit:'+name,'message':'Shorten this text to fit the fixed kiosk area without scrolling or truncation.'})
        if '\n' in text or '\r' in text:
            hard.append({'id':'kiosk:'+name,'message':'Use a single plain-text line; the kiosk cannot scroll.'})
    prose=' '.join([parsed.summary,parsed.strip_explanation,parsed.uncertainty,parsed.alternative,*[h.rationale for h in parsed.hours]])
    if re.search(r'\b\d+\s*%\s*(chance|probability)',prose,re.I):
        hard.append({'id':'probability','message':'No calibrated probability evidence was supplied; remove invented percentages.'})
    if re.search(r'\b(currently raining|raining now|it is raining|rain is falling)\b',prose,re.I):
        hard.append({'id':'instant_rain','message':'Gauge values cover a past interval, not instantaneous rain. Describe recent rain or a prediction.'})
    for h in parsed.hours:
        base=expected[h.hour_offset]
        differences=[]
        if h.temperature_c is not None and base['temperature_c'] is not None and abs(h.temperature_c-base['temperature_c'])>3:
            differences.append(('temperature_departure','Temperature differs by over 3 C from MET; justify local correction and lead-time persistence.'))
        if h.precipitation_mm is not None and (base['precipitation_mm'] is None or abs(h.precipitation_mm-base['precipitation_mm'])>.1):
            differences.append(('precipitation_departure','Rain amount departs from MET or fills missing guidance; justify movement/timing evidence or revise to uncertainty/null. Pressure/RH alone do not fix amount or onset.'))
        if h.wind_speed_kmh is not None and base['wind_speed_kmh'] is not None and abs(h.wind_speed_kmh-base['wind_speed_kmh'])>15:
            differences.append(('wind_departure','Wind differs by over 15 km/h from MET; justify exposure/flow evidence and avoid unbounded persistence.'))
        if h.wind_direction_deg is not None and base['wind_direction_deg'] is not None and abs((h.wind_direction_deg-base['wind_direction_deg']+180)%360-180)>=45:
            differences.append(('wind_direction_departure','Wind direction differs by at least 45 degrees from MET; justify the local flow/transition evidence.'))
        if h.confidence=='high' and payload['limitations']:
            differences.append(('high_support','Explain high support despite the flagged/missing inputs, or lower it.'))
        for name,message in differences:
            advisory.append({'id':f'h{h.hour_offset}.{name}','message':message})
    responses={r.challenge_id:r for r in parsed.advisory_responses}
    pending=[]
    for issue in advisory:
        response=responses.get(issue['id'])
        if issue['id'] not in challenged or response is None or response.action!='justified' or len(response.explanation)<40:
            pending.append(issue)
    return parsed,hard,pending


def chat(messages,timeout):
    endpoint=os.environ['LLM_ENDPOINT'];model=os.environ['LLM_MODEL'];key=os.getenv('LLM_API_KEY','')
    request_body=dict(model=model,messages=messages,temperature=.2,max_tokens=3500,response_format={'type':'json_object'})
    if 'deepseek.com' in endpoint:
        request_body['thinking']={'type':'disabled'}
    body=json.dumps(request_body,allow_nan=False).encode()
    headers={'Content-Type':'application/json'}
    if key:headers['Authorization']='Bearer '+key
    request=urllib.request.Request(endpoint,data=body,headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response:
            raw=response.read(262145)
        if len(raw)>262144:raise ValueError('LLM response exceeded size bound')
        result=json.loads(raw)
        choice=result['choices'][0]
        if choice.get('finish_reason')=='length':raise ValueError('LLM response was truncated; increase output budget or shorten response')
        if not isinstance(choice['message']['content'],str):raise ValueError('LLM returned no final text')
        return choice['message']['content'],{'model':result.get('model',model),'usage':result.get('usage',{})}
    except urllib.error.HTTPError as exc:
        # Never log response bodies/headers that might echo credentials.
        raise RuntimeError('LLM endpoint HTTP '+str(exc.code)) from None


def interpret(payload,transport=None):
    transport=transport or chat
    messages=[{'role':'system','content':SYSTEM},{'role':'user','content':json.dumps(dict(input=payload,response_schema=Interpretation.model_json_schema()),allow_nan=False)}]
    attempts=[];challenged=set();started=time.monotonic()
    for number in range(3):
        remaining=80-(time.monotonic()-started)
        if remaining<=0:break
        try:
            content,metadata=transport(messages,min(25,remaining))
        except (OSError,ValueError,KeyError,RuntimeError) as exc:
            attempts.append(dict(attempt=number+1,error=type(exc).__name__+': '+str(exc)[:160]))
            return None,dict(status='unavailable',attempts=attempts,reason=('Interpreter transport failed: '+str(exc))[:115])
        try:
            raw=json.loads(content)
            parsed,hard,advisory=validate(raw,payload,challenged)
        except (ValueError,TypeError) as exc:
            parsed,hard,advisory=None,[{'id':'json','message':'Return one valid JSON object, not markdown/prose.'}],[]
        attempts.append(dict(attempt=number+1,response=content,metadata=metadata,hard=hard,advisory=advisory))
        if parsed is not None and not hard and not advisory:
            return parsed,dict(status='validated',attempts=attempts,reason=None)
        challenged.update(issue['id'] for issue in advisory)
        messages.extend([{'role':'assistant','content':content},
            {'role':'user','content':json.dumps(dict(action='revise_or_justify',hard_constraints=hard,
                advisory_challenges=advisory,instructions='Fix all hard constraints. Revise advisory departures or explicitly justify each challenge_id with valid references. Shorten kiosk text if needed. Do not substitute deterministic results.'),allow_nan=False)}])
    return None,dict(status='disputed',attempts=attempts,reason='No response satisfied the bounded correction loop')


def audit(payload,trace):
    path=Path(os.getenv('LLM_AUDIT_DB','/var/lib/weatherdash/llm_audit.sqlite3'))
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS runs(input_id TEXT PRIMARY KEY, as_of TEXT, payload_json TEXT, trace_json TEXT)')
        db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?,?)',(payload['input_id'],payload['as_of'],json.dumps(payload,allow_nan=False),json.dumps(trace,allow_nan=False)))


def apply_interpretation(strip,payload,result,trace):
    model = trace['attempts'][-1].get('metadata',{}).get('model',os.getenv('LLM_MODEL','unconfigured')) if trace['attempts'] else os.getenv('LLM_MODEL','unconfigured')
    strip.model_version='llm_v1_'+re.sub('[^a-zA-Z0-9_]+','_',model)[:40]
    strip.synthesis.engine='llm-runtime'
    strip.synthesis.advisory_summary=strip.synthesis.summary
    strip.interpreter_status=trace['status']
    strip.interpreter_input_id=payload['input_id']
    strip.interpreter_attempts=len(trace['attempts'])
    strip.interpreter_model=model
    strip.interpreter_input_as_of=payload['as_of']
    strip.interpreter_issues=[issue for attempt in trace['attempts'] for issue in attempt.get('hard',[])+attempt.get('advisory',[])]
    if result is None:
        strip.degraded=True
        strip.synthesis.summary='Runtime LLM interpretation unavailable or disputed. No validated local six-hour forecast is being substituted.'
        strip.synthesis.strip_explanation='Interpretation failed validation or connection. Current instrument readings remain available; the six-hour interpretation needs a successful update.'
        strip.synthesis.uncertainty=trace['reason']
        strip.synthesis.windows=[]
        strip.synthesis.conflicts=[]
        strip.synthesis.limitations=[trace['reason']]
        for slot in strip.slots[1:]:
            slot.temperature_c=slot.wind_speed_kmh=slot.wind_dir_deg=slot.precipitation_mm=None
            slot.forecast_pressure_hpa=slot.forecast_humidity_pct=None
            slot.state=AtmosphericState.UNKNOWN;slot.state_label='Interpretation unavailable'
            slot.source='llm-unavailable';slot.degraded=True;slot.confidence=0
            slot.confidence_kind='unavailable';slot.evidence=[trace['reason']]
        return strip
    strip.synthesis.summary=result.summary
    strip.synthesis.strip_explanation=result.strip_explanation
    strip.synthesis.uncertainty=result.uncertainty
    strip.synthesis.alternative=result.alternative
    strip.synthesis.model_local_agreement=result.model_local_agreement
    strip.synthesis.conflicts=[r.explanation for r in result.advisory_responses if r.action=='justified']
    # Deterministic regimes remain diagnostics, not the interpreter's conclusion.
    strip.synthesis.windows=[]
    from forecast_state import ForecastWindow
    for hour in sorted(result.hours,key=lambda h:h.hour_offset):
        slot=next(s for s in strip.slots if s.hour_offset==hour.hour_offset)
        slot.temperature_c=hour.temperature_c;slot.wind_speed_kmh=hour.wind_speed_kmh
        slot.wind_dir_deg=hour.wind_direction_deg;slot.precipitation_mm=hour.precipitation_mm
        slot.state=hour.state;slot.state_label=hour.label;slot.source='llm-runtime'
        slot.forecast_valid_time=hour.valid_time
        slot.forecast_pressure_hpa=None;slot.forecast_humidity_pct=None
        slot.confidence=0;slot.confidence_kind='llm_qualitative';slot.confidence_level=hour.confidence
        slot.evidence=[hour.rationale,*hour.evidence_references]
        slot.temperature_adjustment_c=(hour.temperature_c-slot.baseline_temperature_c) if hour.temperature_c is not None and slot.baseline_temperature_c is not None else 0
        slot.degraded=hour.confidence=='low' or hour.temperature_c is None
        if hour.precipitation_mm is not None:
            end=datetime.fromisoformat(hour.valid_time)+timedelta(hours=1)
            end=min(end,datetime.fromisoformat(payload['as_of'])+timedelta(hours=6))
            strip.synthesis.windows.append(ForecastWindow(start=hour.valid_time,end=end.isoformat(),state='wet' if hour.precipitation_mm>0 else 'dry',source='llm-runtime',timing_basis='LLM interpretation for this hourly interval, not exact onset'))
    strip.degraded=any(s.degraded for s in strip.slots)
    return strip


def run_runtime(strip,roof_history,rain_history,pressure_history,transport=None,write_audit=True):
    payload=make_payload(strip,roof_history,rain_history,pressure_history)
    result,trace=interpret(payload,transport)
    published=datetime.now(timezone.utc)
    if result is not None and any(datetime.fromisoformat(h.valid_time)<=published for h in result.hours):
        result=None
        trace.update(status='disputed',reason='Interpretation arrived after its first target time; await fresh inputs')
    if write_audit:audit(payload,trace)
    strip=apply_interpretation(strip,payload,result,trace)
    strip.synthesis.valid_until=(published+timedelta(minutes=10 if result else 5)).isoformat()
    strip.generated_at_iso=published.isoformat()
    from zoneinfo import ZoneInfo
    strip.generated_at=published.astimezone(ZoneInfo('Pacific/Auckland')).strftime('%d/%m %H:%M NZ')
    return strip
