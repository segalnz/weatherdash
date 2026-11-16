"""Scenario tests for expert evidence, time semantics and safe narrative output."""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
import tempfile
import json
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'venvs/barometer'), str(ROOT/'venvs')]
from forecast_state import RooftopSnapshot, DerivedFeatures, ForecastStrip
from synthesis import synthesise, model_windows
from derived_features import compute_derived_features
import main
import snapshot_forecast

NOW = datetime(2026,11,15,1,25,tzinfo=timezone.utc)


def snapshot(**kw):
    return RooftopSnapshot(**(dict(pressure_hpa=1004, temperature_c=15, humidity_pct=70,
        humidity_quality='ok', wind_speed_kmh=12, wind_direction_deg=270,
        rain_quality='ok', rain_recent_mm=0, observation_times={
            k: NOW.isoformat() for k in ['pressure','temperature','humidity','speed','direction']}) | kw))


def model(amount=0):
    return {h: dict(valid_time=(NOW.replace(minute=0)+timedelta(hours=h)).isoformat(),
        precipitation=amount, precipitation_period_hours=1) for h in range(7)}


class SynthesisTests(unittest.TestCase):
    def setUp(self):
        guard = patch.dict('os.environ', {'LLM_ENABLED': 'false'})
        guard.start()
        self.addCleanup(guard.stop)

    def test_related_pressure_signals_are_one_dependency_group(self):
        x=synthesise(snapshot(),DerivedFeatures(pressure_delta_1h=-2,pressure_delta_3h=-2),model(),NOW)
        r=next(r for r in x.regimes if r.id=='deteriorating')
        self.assertEqual(r.support,'limited')
        self.assertEqual(r.dependency_groups,['pressure'])
        self.assertIn('pressure_accelerating',r.evidence_ids)
        self.assertEqual(x.model_local_agreement,'mixed_signals')
        self.assertTrue(x.conflicts)
        self.assertTrue(all(w.state=='dry' for w in x.windows))

    def test_moisture_corroborates_without_inventing_rain(self):
        x=synthesise(snapshot(),DerivedFeatures(pressure_delta_3h=-2,dewpoint_delta_1h=1),model(),NOW)
        self.assertEqual(x.regimes[0].support,'corroborated')
        self.assertTrue(all(w.state=='dry' for w in x.windows))

    def test_cooling_humidity_is_not_moisture_gain(self):
        x=synthesise(snapshot(),DerivedFeatures(humidity_delta_1h=10,temp_delta_1h=-1,dewpoint_delta_1h=0),model(),NOW)
        ids={e.id for e in x.evidence}
        self.assertIn('humidity_from_cooling',ids)
        self.assertNotIn('moisture_increasing',ids)
        self.assertFalse(any(r.id=='deteriorating' for r in x.regimes))

    def test_saturated_or_recovering_humidity_cannot_corroborate(self):
        for quality in ['possible_condensation','invalid','unknown']:
            x=synthesise(snapshot(humidity_pct=99,humidity_quality=quality,wind_speed_kmh=2),
                DerivedFeatures(pressure_delta_3h=-2,dewpoint_delta_1h=2),model(),NOW)
            self.assertEqual(x.regimes[0].support,'limited')
            self.assertNotIn('mist_potential',{r.id for r in x.regimes})

    def test_stale_pressure_does_not_support_regime(self):
        s=snapshot();s.observation_times['pressure']=(NOW-timedelta(minutes=10)).isoformat()
        x=synthesise(s,DerivedFeatures(pressure_delta_3h=-5),model(),NOW)
        self.assertNotIn('deteriorating',{r.id for r in x.regimes})
        self.assertIn('pressure_unavailable',{e.id for e in x.evidence})

    def test_calm_direction_shift_does_not_confirm_front(self):
        x=synthesise(snapshot(wind_speed_kmh=1),DerivedFeatures(pressure_delta_1h=1,pressure_delta_3h=2,temp_delta_1h=-2,wind_dir_change_1h=90),model(),NOW)
        self.assertNotIn('possible_frontal_passage',{r.id for r in x.regimes})

    def test_credible_transition_is_possible_not_confirmed(self):
        x=synthesise(snapshot(),DerivedFeatures(pressure_delta_1h=1,pressure_delta_3h=2,temp_delta_1h=-2,wind_dir_change_1h=90),model(.3),NOW)
        r=next(r for r in x.regimes if r.id=='possible_frontal_passage')
        self.assertEqual(r.support,'possible')
        self.assertIn('temperature_cooling',r.evidence_ids)
        self.assertTrue(all(w.state=='wet' for w in x.windows))

    def test_model_windows_clip_six_hours_and_preserve_gaps(self):
        raw=model();raw[2]['precipitation']=1;raw.pop(3)
        original=json.dumps(raw,sort_keys=True)
        windows=model_windows(raw,NOW)
        self.assertEqual(windows[0].start,NOW.isoformat())
        self.assertEqual(windows[-1].end,(NOW+timedelta(hours=6)).isoformat())
        self.assertEqual(len(windows),3)
        self.assertNotEqual(windows[1].end,windows[2].start)
        self.assertEqual(json.dumps(raw,sort_keys=True),original)
        self.assertTrue(synthesise(snapshot(),DerivedFeatures(),raw,NOW).limitations)

    def test_missing_or_six_hour_precipitation_not_dry(self):
        for raw in [{}, {1:{'valid_time':NOW.isoformat(),'precipitation':6,'precipitation_period_hours':6}}]:
            x=synthesise(snapshot(),DerivedFeatures(),raw,NOW)
            self.assertEqual(x.windows,[])
            self.assertIn('cannot be classified',x.summary)

    def test_recent_rain_does_not_override_dry_future(self):
        x=synthesise(snapshot(rain_recent_mm=2,rain_detected=True),DerivedFeatures(),model(),NOW)
        self.assertIn('recent_rain',{e.id for e in x.evidence})
        self.assertTrue(all(w.state=='dry' for w in x.windows))

    def test_wind_change_requires_noncalm_continuous_windows(self):
        times=pd.date_range(NOW-timedelta(minutes=70),NOW,freq='5min')
        df=pd.DataFrame({'_time':times,'speed':[4]*len(times),'direction':[0 if t<NOW-timedelta(minutes=30) else 90 for t in times]})
        rain=pd.DataFrame()
        self.assertAlmostEqual(compute_derived_features(df,rain,NOW).wind_dir_change_1h,90)
        df['speed']=.5
        self.assertIsNone(compute_derived_features(df,rain,NOW).wind_dir_change_1h)
        self.assertIsNone(compute_derived_features(df.iloc[[0,-1]],rain,NOW).wind_dir_change_1h)

    def test_old_cache_compatible_and_expired_synthesis_withheld(self):
        s=ForecastStrip(generated_at='now',generated_at_iso=datetime.now(timezone.utc).isoformat())
        self.assertIsNone(s.synthesis)
        s.synthesis=synthesise(snapshot(),DerivedFeatures(),model(),NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'cache.json';path.write_text(s.model_dump_json())
            with patch.dict('os.environ',{'FORECAST_CACHE':str(path)}):
                result=main._display_forecast()
            self.assertIsNone(result.synthesis)

    def test_synthesis_preserves_numeric_met_baseline_and_existing_correction(self):
        import math
        raw=model()
        for h,row in raw.items():
            row.update(temp=10,wind_speed=2,wind_dir=180,precipitation=.3 if h==3 else 0,symbol_code='fair_day')
        with patch.multiple(main,get_latest_rooftop=lambda:snapshot(temperature_c=12),
                get_recent_hours_data=lambda **kw:pd.DataFrame(),get_rainfall_series=lambda **kw:pd.DataFrame(),
                get_recent_pressure=lambda **kw:pd.DataFrame(),get_raw_metno_fields=lambda now:raw,
                compute_derived_features=lambda *a,**kw:DerivedFeatures(pressure_delta_3h=.2,temp_delta_1h=0)):
            strip=main._build_forecast()
        generated=datetime.fromisoformat(strip.generated_at_iso)
        for slot in strip.slots[1:]:
            lead=(generated.replace(minute=0,second=0,microsecond=0)+timedelta(hours=slot.hour_offset)-generated).total_seconds()/3600
            self.assertAlmostEqual(slot.temperature_c,10+2*math.exp(-lead/2))
            self.assertEqual(slot.wind_speed_kmh,7.2)
            self.assertEqual(slot.baseline_wind_speed_kmh,7.2)
            self.assertEqual(slot.precipitation_mm,.3 if slot.hour_offset==3 else 0)
            self.assertEqual(slot.precipitation_mm,slot.baseline_precipitation_mm)

    def test_archive_preserves_synthesis_observations_and_features(self):
        s=ForecastStrip(generated_at='now',generated_at_iso=NOW.isoformat(),
            rooftop=snapshot(),derived=DerivedFeatures(pressure_delta_3h=-2),
            synthesis=synthesise(snapshot(),DerivedFeatures(pressure_delta_3h=-2),model(),NOW))
        with tempfile.TemporaryDirectory() as tmp, patch.object(snapshot_forecast,'_get_client') as client, patch.object(snapshot_forecast,'CACHE',Path(tmp)/'forecast.json'):
            snapshot_forecast.archive(s)
            points=client.return_value.__enter__.return_value.write_api.return_value.__enter__.return_value.write.call_args.kwargs['record']
            line=points[0].to_line_protocol()
            self.assertIn('forecast_synthesis',line)
            self.assertIn('observations_json=',line)
            self.assertIn('features_json=',line)
            self.assertIn('payload_json=',line)

if __name__=='__main__':unittest.main()
