import sys, unittest, tempfile, json
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'venvs'),str(ROOT/'venvs/barometer')]
import forecast_processor as fp
import snapshot_forecast
import main
from forecast_state import ForecastSlot, ForecastStrip, AtmosphericState, DerivedFeatures
from test_synthesis import snapshot

VALID=datetime(2026,11,16,1,0,tzinfo=timezone.utc)
ISSUED=VALID-timedelta(minutes=30)
NOW=VALID+timedelta(hours=1,minutes=5)


def issued(**kwargs):
    return dict(_time=ISSUED,valid_time=VALID.timestamp(),model_version='test',horizon='1h',
        temp=13,baseline_temp=14,persistence_temp=11,persistence_temp_time=(ISSUED-timedelta(seconds=30)).timestamp(),
        wind_speed=4,baseline_wind_speed=3,wind_dir=10,baseline_wind_dir=10,
        pressure=1001,humidity=75,precipitation=.4,baseline_precipitation=.4,
        persistent_regimes_json='[]') | kwargs


def observations():
    return pd.DataFrame([
        dict(_time=VALID,topic='sht45/telemetry',temperature=12,humidity=74,quality_valid=True),
        dict(_time=VALID,topic='status/weather/data',speed=3,direction=350),
        dict(_time=VALID,topic='pressure/telemetry',station='local-pressure',pressure=1000,quality_valid=True,pressure_basis='legacy_sea_level_corrected')])


def gauge(amount=.1):
    return pd.DataFrame(dict(_time=pd.date_range(VALID,periods=6,freq='10min'),rainfall_mm=[amount]*6,slots_missing=[0]*6,station=['davis-rainbucket']*6))


class VerificationTests(unittest.TestCase):
    def test_errors_and_baselines_use_same_matched_observation(self):
        result=fp.verify(issued(),observations(),gauge(),NOW)
        self.assertEqual(result['temp_error'],-1)
        self.assertEqual(result['baseline_temp_error'],-2)
        self.assertEqual(result['persistence_temp_error'],1)
        self.assertEqual(result['wind_dir_error'],-20)
        self.assertAlmostEqual(result['precipitation_error'],.2)
        self.assertEqual(result['lead_minutes'],30)

    def test_latest_invalid_masks_older_good_temperature_and_pressure(self):
        frame=observations();frame['_time']=VALID-timedelta(minutes=1)
        invalid=pd.DataFrame([dict(_time=VALID,topic='sht45/telemetry',quality_valid=False),dict(_time=VALID,topic='pressure/telemetry',station='local-pressure',quality_valid=False)])
        result=fp.verify(issued(),pd.concat([frame,invalid],ignore_index=True),gauge(),NOW)
        self.assertFalse(result['temp_verified'])
        self.assertFalse(result['pressure_verified'])
        self.assertNotIn('baseline_temp_error',result)

    def test_ten_minute_old_and_after_valid_truth_are_excluded(self):
        for delta in [timedelta(minutes=-10),timedelta(seconds=1)]:
            frame=observations();frame['_time']=VALID+delta
            result=fp.verify(issued(),frame,gauge(),NOW)
            self.assertFalse(result['temp_verified'])
            self.assertFalse(result['pressure_verified'])

    def test_wrong_pressure_source_is_not_used(self):
        frame=observations();frame.loc[frame.topic=='pressure/telemetry','station']='old-sensor'
        self.assertFalse(fp.verify(issued(),frame,gauge(),NOW)['pressure_verified'])

    def test_calm_wind_direction_not_scored_but_zero_speed_is(self):
        frame=observations();frame.loc[frame.topic=='status/weather/data','speed']=0
        result=fp.verify(issued(wind_speed=0),frame,gauge(),NOW)
        self.assertTrue(result['wind_speed_verified']);self.assertEqual(result['wind_speed_error'],0)
        self.assertFalse(result['wind_dir_verified'])

    def test_conflicting_same_time_ground_truth_is_not_arbitrarily_selected(self):
        frame=observations();extra=frame.iloc[[0]].copy();extra['temperature']=30
        result=fp.verify(issued(),pd.concat([frame,extra],ignore_index=True),gauge(),NOW)
        self.assertFalse(result['temp_verified'])

    def test_rain_requires_whole_correct_complete_hour(self):
        for rain in [gauge().iloc[:-1],gauge().assign(slots_missing=1),pd.concat([gauge(),gauge().iloc[[0]]]),gauge().assign(station='other')]:
            self.assertFalse(fp.verify(issued(),observations(),rain,NOW)['precipitation_verified'])
        self.assertFalse(fp.verify(issued(),observations(),gauge(),VALID+timedelta(minutes=59))['precipitation_verified'])
        self.assertTrue(fp.verify(issued(),observations(),gauge(0),NOW)['precipitation_verified'])

    def test_no_training_leak_from_future_or_missing_persistence_time(self):
        for stamp in [None,ISSUED.timestamp()+1,ISSUED.timestamp()-600]:
            result=fp.verify(issued(persistence_temp_time=stamp),observations(),gauge(),NOW)
            self.assertNotIn('persistence_temp_error',result)
        row=issued();row.pop('persistence_temp');row.pop('persistence_temp_time')
        self.assertNotIn('persistence_temp_error',fp.verify(row,observations(),gauge(),NOW))
        self.assertEqual(fp.verify(issued(_time=VALID),observations(),gauge(),NOW),{})

    def test_report_comparisons_are_paired_not_unmatched_averages(self):
        a=issued();first=fp.verify(a,observations(),gauge(),NOW)
        b=issued(baseline_temp=None);second=fp.verify(b,observations(),gauge(),NOW);second['temp_error']=100
        report=fp.build_report([(a,first),(b,second)],NOW,72)
        cohort=next(c for c in report['cohorts'] if c['regime']=='all')
        scores=cohort['variables']['temp']
        self.assertEqual(scores['methods']['weatherdash']['count'],2)
        self.assertEqual(scores['paired']['metno']['count'],1)
        self.assertEqual(scores['paired']['metno']['mae_improvement'],1)
        self.assertEqual(cohort['precipitation_contingency']['hits'],2)
        json.dumps(report,allow_nan=False)

    def test_regime_stratification_and_unrecorded_context(self):
        a=issued(persistent_regimes_json='["deteriorating"]');b=issued();b.pop('persistent_regimes_json')
        r=fp.build_report([(a,fp.verify(a,observations(),gauge(),NOW)),(b,fp.verify(b,observations(),gauge(),NOW))],NOW,72)
        self.assertEqual({c['regime'] for c in r['cohorts']},{'all','persistent:deteriorating','unrecorded'})

    def test_rain_contingency_does_not_treat_missing_as_dry(self):
        rows=[]
        for pred,obs in [(1,1),(0,1),(1,0),(0,0)]:
            row=issued(precipitation=pred);rows.append((row,fp.verify(row,observations(),gauge(obs),NOW)))
        row=issued(precipitation=1);rows.append((row,fp.verify(row,observations(),gauge().iloc[:-1],NOW)))
        r=fp.build_report(rows,NOW,72)
        m=next(c for c in r['cohorts'] if c['regime']=='all')['precipitation_contingency']
        self.assertEqual(m,dict(hits=1,misses=1,false_alarms=1,correct_negatives=1))

    def test_issue_archive_freezes_persistence_and_original_timestamp(self):
        obs=snapshot();obs.observation_times={k:(ISSUED-timedelta(seconds=30)).isoformat() for k in obs.observation_times}
        strip=ForecastStrip(generated_at='test',generated_at_iso=ISSUED.isoformat(),rooftop=obs,derived=DerivedFeatures(),
            slots=[ForecastSlot(hour_offset=0,state=AtmosphericState.UNKNOWN,state_label='unknown',icon=''),
                ForecastSlot(hour_offset=1,state=AtmosphericState.CLOUDY,state_label='cloudy',icon='',temperature_c=12,baseline_temperature_c=10,
                    baseline_wind_speed_kmh=7.2,baseline_wind_dir_deg=90,baseline_precipitation_mm=0,forecast_valid_time=VALID.isoformat())])
        with tempfile.TemporaryDirectory() as tmp,patch.object(snapshot_forecast,'CACHE',Path(tmp)/'cache.json'),patch.object(snapshot_forecast,'_get_client') as client:
            snapshot_forecast.archive(strip)
            points=client.return_value.__enter__.return_value.write_api.return_value.__enter__.return_value.write.call_args.kwargs['record']
            line=points[0].to_line_protocol()
            self.assertIn('persistence_temp=15',line)
            self.assertIn('persistence_temp_time=',line)
            self.assertIn('baseline_wind_speed=2',line)
            self.assertIn('baseline_wind_dir=90',line)
            self.assertIn('baseline_precipitation=0',line)

    def test_report_only_does_not_write_influx_or_files(self):
        import contextlib,io
        frames=[pd.DataFrame([issued()]),observations(),gauge()]
        with tempfile.TemporaryDirectory() as tmp,patch.object(fp,'REPORT',Path(tmp)/'report.json') as report_path,patch.object(fp,'InfluxDBClient') as client,patch.object(fp,'read_frame',side_effect=frames),contextlib.redirect_stdout(io.StringIO()):
            fp.main(['--report-only','--hours','24'])
            client.return_value.__enter__.return_value.write_api.assert_not_called()
            self.assertFalse(report_path.exists())

    def test_report_api_handles_missing_stale_and_fresh_without_db_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'report.json'
            with patch.dict('os.environ',{'FORECAST_VERIFICATION_REPORT':str(path)}):
                self.assertEqual(main.api_verification()['status'],'unavailable')
                path.write_text(json.dumps(fp.build_report([],datetime.now(timezone.utc)-timedelta(hours=2),72)))
                self.assertEqual(main.api_verification()['status'],'stale')
                path.write_text(json.dumps(fp.build_report([],datetime.now(timezone.utc),72)))
                self.assertEqual(main.api_verification()['status'],'ok')

if __name__=='__main__':unittest.main()
