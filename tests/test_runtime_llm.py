"""No live API calls: exercise correction, justification, failure and kiosk contracts."""
import sys,unittest,json,copy,tempfile,sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'venvs/barometer'),str(ROOT/'venvs')]
from test_synthesis import snapshot,NOW
from forecast_state import ForecastStrip,ForecastSlot,AtmosphericState,DerivedFeatures
from synthesis import synthesise
import runtime_llm as llm
from svg_renderer import render_forecast_strip


def fixture():
    obs=snapshot();obs.observation_times['gust']=NOW.isoformat();obs.observation_times['rain']=NOW.isoformat()
    obs.rain_interval_start=(NOW-timedelta(minutes=10)).isoformat();obs.rain_interval_end=NOW.isoformat()
    slots=[ForecastSlot(hour_offset=0,state=AtmosphericState.UNKNOWN,state_label='Observed',icon='',temperature_c=15)]
    for h in range(1,7):
        slots.append(ForecastSlot(hour_offset=h,state=AtmosphericState.PARTLY_CLOUDY,state_label='Partly cloudy',icon='',
            temperature_c=16,wind_speed_kmh=12,wind_dir_deg=270,precipitation_mm=0,
            baseline_temperature_c=15,baseline_wind_speed_kmh=12,baseline_wind_dir_deg=270,baseline_precipitation_mm=0,
            met_symbol_code='fair_day',forecast_valid_time=(NOW.replace(minute=0)+timedelta(hours=h)).isoformat()))
    strip=ForecastStrip(generated_at='test',generated_at_iso=NOW.isoformat(),rooftop=obs,derived=DerivedFeatures(pressure_delta_3h=-2,wind_speed_delta_1h=1),
        slots=slots,synthesis=synthesise(obs,DerivedFeatures(pressure_delta_3h=-2),{},NOW))
    payload=llm.make_payload(strip,pd.DataFrame(),pd.DataFrame(),pd.DataFrame())
    response=dict(input_id=payload['input_id'],summary='Mostly dry conditions are expected, with uncertain local cloud changes.',
        strip_explanation='MET dry guidance is weighed against falling local pressure; this does not establish rain arrival.',
        uncertainty='Local history is incomplete and rain arrival cannot be timed independently.',alternative='An isolated shower remains an alternative if the regional evolution changes.',
        model_local_agreement='mixed',claims=[dict(reference='observations.temperature_c',value=15)],
        acknowledged_limitations=[l['id'] for l in payload['limitations']],advisory_responses=[],
        hours=[dict(hour_offset=r['hour_offset'],valid_time=r['valid_time'],state='partly_cloudy',label='Partly cloudy',temperature_c=15,
                    wind_speed_kmh=12,wind_direction_deg=270,precipitation_mm=0,confidence='medium',
                    rationale='The regional guidance remains dry; local pressure alone cannot locate incoming rain.',
                    evidence_references=[f'met_forecast.{i}.temperature_c']) for i,r in payload['met_forecast'].items()])
    return strip,payload,response


def transport(responses):
    responses=iter(responses);calls=[]
    def call(messages,timeout):
        calls.append(copy.deepcopy(messages));return json.dumps(next(responses)),{'model':'test-model','usage':{'total_tokens':10}}
    return call,calls


class RuntimeLLMTests(unittest.TestCase):
    def test_valid_interpreter_is_authoritative_not_deterministic_substitution(self):
        strip,payload,response=fixture();call,calls=transport([response])
        result,trace=llm.interpret(payload,call)
        self.assertEqual(trace['status'],'validated');self.assertEqual(len(calls),1)
        llm.apply_interpretation(strip,payload,result,trace)
        self.assertEqual(strip.slots[1].temperature_c,15)  # deterministic candidate was 16
        self.assertEqual(strip.slots[1].source,'llm-runtime')
        self.assertEqual(strip.synthesis.summary,response['summary'])
        self.assertEqual(strip.interpreter_model,'test-model')
        self.assertIn('Support: medium',render_forecast_strip(strip))
        self.assertNotIn('…', ' '.join(llm.textwrap.wrap(response['strip_explanation'],width=140)))

    def test_invented_observation_is_sent_back_and_corrected(self):
        _,payload,response=fixture();bad=copy.deepcopy(response);bad['claims'][0]['value']=99
        call,calls=transport([bad,response]);result,trace=llm.interpret(payload,call)
        self.assertIsNotNone(result);self.assertEqual(len(trace['attempts']),2)
        feedback=json.loads(calls[1][-1]['content'])
        self.assertTrue(any(i['id'].startswith('claim:') for i in feedback['hard_constraints']))
        self.assertEqual(calls[1][-2]['role'],'assistant')

    def test_advisory_departure_may_be_justified_without_replacement(self):
        strip,payload,response=fixture();response['hours'][0]['temperature_c']=19
        justified=copy.deepcopy(response);justified['advisory_responses']=[dict(challenge_id='h1.temperature_departure',action='justified',
            explanation='The local observation and recent history support retaining a warmer local value briefly; confidence remains limited.',
            evidence_references=['observations.temperature_c'])]
        call,calls=transport([response,justified]);result,trace=llm.interpret(payload,call)
        self.assertEqual(trace['status'],'validated');self.assertEqual(len(calls),2)
        llm.apply_interpretation(strip,payload,result,trace)
        self.assertEqual(strip.slots[1].temperature_c,19)  # not MET 15 and not deterministic 16
        self.assertTrue(strip.synthesis.conflicts)

    def test_unchanged_dispute_is_not_silently_replaced(self):
        strip,payload,response=fixture();response['hours'][0]['temperature_c']=19
        call,_=transport([response]*3);result,trace=llm.interpret(payload,call)
        self.assertIsNone(result);self.assertEqual(trace['status'],'disputed')
        llm.apply_interpretation(strip,payload,result,trace)
        self.assertIsNone(strip.slots[1].temperature_c)
        self.assertEqual(strip.slots[1].baseline_temperature_c,15)
        self.assertEqual(strip.slots[1].state,AtmosphericState.UNKNOWN)
        self.assertEqual(strip.slots[0].temperature_c,15)
        self.assertIn('No validated',strip.synthesis.summary)

    def test_transport_failure_is_visible_and_bounded(self):
        strip,payload,_=fixture()
        def fail(*args):raise RuntimeError('LLM endpoint HTTP 503')
        result,trace=llm.interpret(payload,fail)
        self.assertEqual(trace['status'],'unavailable');self.assertEqual(len(trace['attempts']),1)
        llm.apply_interpretation(strip,payload,result,trace)
        self.assertIn('unavailable',strip.synthesis.summary)

    def test_wrong_request_time_and_duplicate_hour_are_hard_constraints(self):
        _,payload,response=fixture()
        for mutation in ['input','time','hour']:
            bad=copy.deepcopy(response)
            if mutation=='input':bad['input_id']='different'
            if mutation=='time':bad['hours'][0]['valid_time']=(NOW+timedelta(days=1)).isoformat()
            if mutation=='hour':bad['hours'][1]['hour_offset']=1
            _,hard,_=llm.validate(bad,payload,set())
            self.assertTrue(hard,mutation)

    def test_hard_errors_cannot_be_justified_away(self):
        _,payload,response=fixture();response['hours'][0]['wind_speed_kmh']=999
        response['advisory_responses']=[dict(challenge_id='schema',action='justified',explanation='A lengthy explanation cannot make an impossible value valid.',evidence_references=['observations.wind_speed_kmh'])]
        call,_=transport([response]*3);result,trace=llm.interpret(payload,call)
        self.assertIsNone(result);self.assertEqual(trace['status'],'disputed')

    def test_missing_and_null_evidence_are_not_numeric_support(self):
        _,payload,response=fixture();bad=copy.deepcopy(response)
        bad['hours'][0]['evidence_references']=['history.pressure_hpa.points.0.value']
        _,hard,_=llm.validate(bad,payload,set());self.assertTrue(hard)
        payload['observations']['temperature_c']=None
        _,hard,_=llm.validate(response,payload,set());self.assertTrue(any(i['id'].startswith('claim:') for i in hard))

    def test_long_kiosk_text_requires_model_revision(self):
        _,payload,response=fixture();bad=copy.deepcopy(response);bad['strip_explanation']='Long supporting evidence. '*30
        call,calls=transport([bad,response]);result,trace=llm.interpret(payload,call)
        self.assertIsNotNone(result);self.assertEqual(len(calls),2)
        self.assertTrue(trace['attempts'][0]['hard'])

    def test_limitations_and_instantaneous_rain_claims_are_checked(self):
        _,payload,response=fixture();bad=copy.deepcopy(response)
        bad['acknowledged_limitations']=[];bad['summary']='It is raining now with an 80% chance of more rain.'
        _,hard,_=llm.validate(bad,payload,set());ids={i['id'] for i in hard}
        self.assertTrue({'limitations','instant_rain','probability'}.issubset(ids))

    def test_payload_units_gauge_intervals_and_no_nan(self):
        _,payload,_=fixture()
        self.assertEqual(payload['trends']['wind_speed_delta_1h'],3.6)
        self.assertIsNotNone(payload['observations']['rain_interval_start'])
        self.assertIsNone(payload['lux_expected_current'])
        json.dumps(payload,allow_nan=False)
        self.assertEqual(payload['met_forecast']['1']['symbol_code'],'fair_day')

    def test_all_findings_returned_when_text_and_facts_are_wrong(self):
        _,payload,response=fixture();response['uncertainty']='Too much explanation. '*20;response['claims'][0]['value']=99
        _,hard,_=llm.validate(response,payload,set())
        self.assertIn('schema:uncertainty',{h['id'] for h in hard})
        self.assertIn('claim:observations.temperature_c',{h['id'] for h in hard})

    def test_boolean_is_not_a_numeric_prediction(self):
        _,payload,response=fixture();response['hours'][0]['temperature_c']=True
        _,hard,_=llm.validate(response,payload,set());self.assertTrue(hard)

    def test_zero_and_null_predictions_remain_zero_and_unknown(self):
        _,payload,response=fixture()
        for h in range(1,7):
            for field in ['temperature_c','wind_speed_kmh','wind_direction_deg','precipitation_mm']:
                payload['met_forecast'][str(h)][field]=0;response['hours'][h-1][field]=0
        parsed,hard,advisory=llm.validate(response,payload,set())
        self.assertFalse(hard);self.assertFalse(advisory);self.assertEqual(parsed.hours[0].temperature_c,0)
        for hour in response['hours']:
            hour.update(state='unknown',temperature_c=None,wind_speed_kmh=None,wind_direction_deg=None,precipitation_mm=None,evidence_references=[],confidence='low')
        parsed,hard,_=llm.validate(response,payload,set())
        self.assertFalse(hard);self.assertIsNone(parsed.hours[0].temperature_c)

    def test_late_result_is_not_backdated_as_an_issued_forecast(self):
        strip,_,response=fixture();call,_=transport([response])
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return NOW+timedelta(hours=2)
        with patch.object(llm,'datetime',Clock):
            result=llm.run_runtime(strip,pd.DataFrame(),pd.DataFrame(),pd.DataFrame(),transport=call,write_audit=False)
        self.assertEqual(result.interpreter_status,'disputed')
        self.assertEqual(result.generated_at_iso,(NOW+timedelta(hours=2)).isoformat())
        self.assertIsNone(result.slots[1].temperature_c)

    def test_display_reads_cache_without_calling_interpreter(self):
        import main
        strip,_,_=fixture();strip.generated_at_iso=datetime.now().astimezone().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'cache.json';file.write_text(strip.model_dump_json())
            with patch.dict('os.environ',{'FORECAST_CACHE':str(file)}),patch.object(llm,'chat') as provider:
                main.api_forecast();provider.assert_not_called()

    def test_history_excludes_future_bucket_ends_and_incomplete_rain(self):
        frame=pd.DataFrame({'_time':[NOW-timedelta(minutes=5),NOW+timedelta(minutes=5)],'temperature':[12,99]})
        data=llm.series(frame,'temperature','C',5,as_of=NOW)
        self.assertEqual([p['value'] for p in data['points']],[12])
        frame=pd.DataFrame({'_time':[NOW-timedelta(minutes=10),NOW-timedelta(minutes=5)],'rainfall_mm':[1,99]})
        data=llm.series(frame,'rainfall_mm','mm',10,as_of=NOW,interval_start=True)
        self.assertEqual([p['value'] for p in data['points']],[1])

    def test_audit_contains_initial_response_and_correction(self):
        _,payload,response=fixture();bad=copy.deepcopy(response);bad['claims'][0]['value']=100
        call,_=transport([bad,response]);_,trace=llm.interpret(payload,call)
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'LLM_AUDIT_DB':str(Path(tmp)/'audit.sqlite3')}):
            llm.audit(payload,trace)
            with closing(sqlite3.connect(Path(tmp)/'audit.sqlite3')) as db:
                stored=json.loads(db.execute('SELECT trace_json FROM runs').fetchone()[0])
            self.assertEqual(len(stored['attempts']),2);self.assertTrue(stored['attempts'][0]['hard'])
            self.assertNotIn('Authorization',json.dumps(stored))

if __name__=='__main__':unittest.main()
