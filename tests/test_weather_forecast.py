"""Offline regression tests: python -m unittest discover -s tests -v."""
import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'venvs/barometer'), str(ROOT / 'venvs')]
import pandas as pd
import heuristics
import main
import influx_reader
import metno_forecast
from forecast_state import AtmosphericState as State, RooftopSnapshot, DerivedFeatures
from sensor_quality import assess_humidity
from svg_renderer import render_forecast_strip

NOW = datetime(2026, 9, 30, 0, 25, tzinfo=timezone.utc)
WET = {State.DRIZZLE, State.SHOWERS, State.RAIN_LIKELY, State.HEAVY_RAIN}


def row(offset, issued_age=1, **fields):
    valid = NOW.replace(minute=0) + timedelta(hours=offset)
    return SimpleNamespace(values=dict(_time=valid,
        forecast_issued=(NOW - timedelta(hours=issued_age)).timestamp(), **fields))


class ForecastTests(unittest.TestCase):
    def test_humidity_and_pressure_cannot_invent_rain(self):
        for humidity in (70, 86, 95, 100):
            for pressure in (1008, 1015, 1025):
                for delta in (0, -2.3, -5):
                    with self.subTest(humidity=humidity, pressure=pressure, delta=delta):
                        obs = RooftopSnapshot(humidity_pct=humidity, pressure_hpa=pressure,
                            lux=10000, wind_speed_kmh=3, rain_detected=False)
                        assess_humidity(obs, pd.DataFrame(), NOW)
                        derived = DerivedFeatures(pressure_delta_3h=delta, humidity_delta_1h=10)
                        current = heuristics.compute_now_forecast(obs, derived)
                        self.assertNotIn(current.state, WET)
                        for hour in range(1, 7):
                            result = heuristics._project_horizon(current, derived, hour,
                                dict(precipitation=0, precipitation_period_hours=1, symbol_code='fair_day'))
                            self.assertNotIn(result[0], WET)

    def test_observed_rain_does_not_override_future_dry_hour(self):
        current = heuristics.compute_now_forecast(RooftopSnapshot(rain_detected=True), DerivedFeatures())
        self.assertIn(current.state, WET)
        self.assertNotIn(heuristics._project_horizon(current, DerivedFeatures(), 1,
            dict(precipitation=0, precipitation_period_hours=1))[0], WET)

    def test_model_rain_retained_even_with_dry_gauge(self):
        current = heuristics.compute_now_forecast(RooftopSnapshot(rain_detected=False), DerivedFeatures())
        self.assertIn(heuristics._project_horizon(current, DerivedFeatures(), 3,
            dict(precipitation=0.5, precipitation_period_hours=1))[0], WET)

    def test_missing_and_six_hour_totals_are_not_hourly_predictions(self):
        current = heuristics.compute_now_forecast(RooftopSnapshot(), DerivedFeatures())
        for raw in ({}, {'precipitation':0}, {'precipitation':6,'precipitation_period_hours':6},
                    {'precipitation':float('nan'),'precipitation_period_hours':1}):
            self.assertEqual(heuristics._project_horizon(current, DerivedFeatures(), 6, raw)[1],
                             'Forecast unavailable')

    def test_valid_time_not_original_horizon(self):
        result = influx_reader.select_forecast_rows([row(2, horizon='6h', temp=0)], NOW)
        self.assertEqual(list(result), [2])
        self.assertEqual(result[2]['temp'], 0)

    def test_coherent_run_does_not_fill_holes_from_older_run(self):
        result = influx_reader.select_forecast_rows([row(1,1,temp=10), row(2,2,temp=20)], NOW)
        self.assertEqual(list(result), [1])

    def test_stale_future_issued_and_past_valid_are_rejected(self):
        self.assertEqual(influx_reader.select_forecast_rows(
            [row(1,7), row(2,-1), row(-1,1)], NOW), {})

    def test_reader_queries_future_window(self):
        with patch.object(influx_reader,'query_flux', return_value=[]) as query:
            self.assertEqual(influx_reader.get_raw_metno_fields(NOW), {})
        flux=query.call_args.args[0]
        self.assertIn('stop: 2026-09-30T07:00:00+00:00', flux)
        self.assertNotIn('last()', flux)

    def test_humidity_saturation_is_qualified_not_discarded(self):
        obs=RooftopSnapshot(humidity_pct=99)
        history=pd.DataFrame({'_time':pd.date_range(NOW-timedelta(hours=1),NOW,freq='5min'),
                              'humidity':[99]*13})
        assess_humidity(obs,history,NOW)
        self.assertEqual(obs.humidity_quality,'possible_condensation')
        self.assertEqual(obs.humidity_pct,99)
        self.assertIn('Sustained',obs.humidity_quality_reason)
        obs.humidity_pct=70
        assess_humidity(obs,history,NOW)
        self.assertEqual(obs.humidity_quality,'ok')

    def test_humidity_invalid_or_missing(self):
        for value in (float('nan'),float('inf'),-1,101):
            obs=RooftopSnapshot(humidity_pct=value)
            assess_humidity(obs,pd.DataFrame(),NOW)
            self.assertEqual(obs.humidity_quality,'invalid')
            self.assertIsNone(obs.humidity_pct)
        obs=RooftopSnapshot()
        assess_humidity(obs,pd.DataFrame(),NOW)
        self.assertEqual(obs.humidity_quality,'unknown')

    def test_ingestion_retains_all_near_hours_and_period(self):
        entries=[]
        for hour in range(7):
            entries.append({'time':(NOW.replace(minute=0)+timedelta(hours=hour)).isoformat(),
                'data':{'instant':{'details':{'air_temperature':0}},
                    'next_1_hours':{'details':{'precipitation_amount':0},
                                    'summary':{'symbol_code':'fair_day'}}}})
        points=metno_forecast.build_points({'properties':{'timeseries':entries}},NOW.replace(minute=0))
        self.assertEqual(len(points),7)
        self.assertTrue(all('precipitation_period_hours=1i' in p.to_line_protocol() for p in points))
        self.assertTrue(all('symbol_code="fair_day"' in p.to_line_protocol() for p in points))
        entries[0]['data']['next_6_hours']=entries[0]['data'].pop('next_1_hours')
        point=metno_forecast.build_points({'properties':{'timeseries':entries[:1]}},NOW.replace(minute=0))[0]
        self.assertIn('precipitation_period_hours=6i',point.to_line_protocol())

    def test_legacy_prompt_preserves_dry_precipitation_and_quality(self):
        from llm_reasoning import build_llm_prompt
        obs=RooftopSnapshot(temperature_c=0,humidity_pct=99,wind_speed_kmh=0,
                            humidity_quality="possible_condensation")
        messages=build_llm_prompt(obs,DerivedFeatures(),0,None,"A","Fine",0,50,
                                 9,12,[0]*6,[0]*6,[0]*6)
        content=messages[1]['content']
        self.assertEqual(content.count('precip 0 mm'),6)
        self.assertIn('possible_condensation',content)
        self.assertIn('Temperature: 0.0',content)

    def test_route_uses_model_preserves_zero_and_renders_unknown(self):
        raw={h:dict(temp=0,wind_speed=0,wind_dir=0,precipitation=0,
                    precipitation_period_hours=1,symbol_code='fair_day') for h in range(1,6)}
        with patch.multiple(main,
                get_latest_rooftop=lambda:RooftopSnapshot(humidity_pct=100,pressure_hpa=1008),
                get_recent_hours_data=lambda **kw:pd.DataFrame(),
                get_rainfall_series=lambda **kw:pd.DataFrame(),
                get_recent_pressure=lambda **kw:pd.DataFrame(),
                get_raw_metno_fields=lambda now:raw):
            strip=main._build_forecast()
        for slot in strip.slots[1:6]:
            self.assertNotIn(slot.state,WET)
            self.assertEqual(slot.temperature_c,0)
            self.assertEqual(slot.wind_speed_kmh,0)
        self.assertTrue(strip.slots[6].degraded)
        self.assertEqual(strip.slots[6].state,State.UNKNOWN)
        self.assertIn('Forecast unavailable',render_forecast_strip(strip))
        strip.model_dump_json()

if __name__=='__main__':
    unittest.main()
