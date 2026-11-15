import sys, unittest, tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'venvs/barometer'),str(ROOT/'venvs')]
from test_synthesis import snapshot, model, NOW
from synthesis import synthesise
from forecast_state import DerivedFeatures, ForecastSynthesis, ForecastStrip
import snapshot_forecast


def step(minutes, previous=None, p3=-2, **kwargs):
    now=NOW+timedelta(minutes=minutes)
    obs=snapshot(**kwargs)
    obs.observation_times={k:now.isoformat() for k in obs.observation_times}
    return synthesise(obs,DerivedFeatures(pressure_delta_3h=p3),model(),now,previous)


def established():
    a=step(0);b=step(5,a);return step(10,b)


class TrackingTests(unittest.TestCase):
    def test_requires_duration_and_three_fresh_cycles(self):
        a=step(0);self.assertEqual(a.regimes[0].persistence,'candidate')
        b=step(10,a);self.assertEqual(b.regimes[0].persistence,'candidate')
        c=step(15,b);self.assertEqual(c.regimes[0].persistence,'persistent')
        self.assertEqual(c.regimes[0].fresh_cycles,3)

    def test_serialized_restart_preserves_progress(self):
        a=ForecastSynthesis.model_validate_json(step(0).model_dump_json())
        b=ForecastSynthesis.model_validate_json(step(5,a).model_dump_json())
        c=step(10,b)
        self.assertEqual(c.regimes[0].persistence,'persistent')
        self.assertIsNotNone(c.regimes[0].persistent_since)

    def test_repeated_sensor_packet_is_not_a_new_cycle(self):
        a=step(0)
        for minute in (1,3,5,8):
            obs=snapshot()
            a=synthesise(obs,DerivedFeatures(pressure_delta_3h=-2),model(),NOW+timedelta(minutes=minute),a)
        self.assertEqual(a.regimes[0].fresh_cycles,1)
        self.assertEqual(a.regimes[0].persistence,'candidate')

    def test_exit_band_prevents_threshold_flicker_but_has_expiry(self):
        a=established()
        b=step(15,a,p3=-.8)
        self.assertEqual(b.regimes[0].persistence,'weakening')
        self.assertEqual(b.regimes[0].evidence_ids,[])
        c=step(20,b,p3=-1.1)
        self.assertEqual(c.regimes[0].persistence,'persistent')
        a=step(25,c,p3=-.8);a=step(30,a,p3=-.8);a=step(35,a,p3=-.8)
        self.assertEqual(a.regimes,[])
        self.assertIn('ended',{t.state for t in a.regime_transitions})

    def test_valid_opposing_evidence_ends_after_exit_duration(self):
        a=established()
        a=step(15,a,p3=.3);a=step(20,a,p3=.3)
        self.assertEqual(a.regimes[0].persistence,'weakening')
        a=step(25,a,p3=.3)
        self.assertEqual(a.regimes,[])

    def test_missing_current_evidence_withholds_immediately(self):
        a=established();a=step(15,a,pressure_hpa=None)
        self.assertEqual(a.regimes,[])
        self.assertEqual(a.regime_memory,{})
        self.assertEqual(a.regime_transitions[0].state,'withheld')
        self.assertEqual(step(20,a).regimes[0].fresh_cycles,1)

    def test_long_forecast_gap_resets_established_regime(self):
        a=step(26,established())
        self.assertEqual(a.regimes[0].persistence,'candidate')
        self.assertEqual(a.regimes[0].fresh_cycles,1)

    def test_backwards_time_rejected_and_input_regression_withheld(self):
        a=established()
        with self.assertRaises(ValueError):step(9,a)
        obs=snapshot();obs.observation_times={k:(NOW+timedelta(minutes=15)).isoformat() for k in obs.observation_times}
        obs.observation_times['pressure']=(NOW+timedelta(minutes=9)).isoformat()
        b=synthesise(obs,DerivedFeatures(pressure_delta_3h=-2),model(),NOW+timedelta(minutes=15),a)
        self.assertEqual(b.regimes,[])

    def test_cache_reader_does_not_advance_and_generator_loads_previous(self):
        a=established()
        strip=ForecastStrip(generated_at='test',generated_at_iso=a.as_of,synthesis=a)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'cache.json';path.write_text(strip.model_dump_json())
            with patch.object(snapshot_forecast,'CACHE',path),patch.object(snapshot_forecast,'_build_forecast',return_value=strip) as build,patch.object(snapshot_forecast,'archive'):
                snapshot_forecast.generate()
                previous=build.call_args.kwargs['previous_synthesis']
                self.assertEqual(previous.regime_memory['deteriorating'].fresh_cycles,3)
            self.assertEqual(path.read_text(),strip.model_dump_json())

if __name__=='__main__':unittest.main()
