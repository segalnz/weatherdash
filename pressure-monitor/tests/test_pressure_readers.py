import sys,unittest,importlib.util,math
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'venvs/barometer')]
import pandas as pd
import influx_reader as reader
import main
from forecast_state import ForecastStrip
from svg_renderer import _draw_pressure_graph
from starlette.responses import JSONResponse

class Record:
 def __init__(self,stamp,pressure,valid=True):
  self.values={'_time':stamp,'pressure':pressure,'quality_valid':valid,'pressure_basis':'legacy_sea_level_corrected'}
 def __getitem__(self,k):return self.values[k]

class ReaderTests(unittest.TestCase):
 def setUp(self):
  guard=patch.dict('os.environ',{'LLM_ENABLED':'false'});guard.start();self.addCleanup(guard.stop)

 def test_newer_invalid_masks_old_pressure(self):
  now=datetime.now(timezone.utc)
  with patch.object(reader,'pressure_rows',return_value=[Record(now-timedelta(minutes=5),1004),Record(now,None,False)]),patch.object(reader,'query_flux',return_value=[]):
   self.assertIsNone(reader.get_latest_rooftop().pressure_hpa)
 def test_gaps_and_invalid_bins(self):
  now=datetime(2026,11,15,0,0,tzinfo=timezone.utc)
  rows=[Record(now,1004),Record(now+timedelta(minutes=1),None,False),Record(now+timedelta(minutes=15),1005)]
  with patch.object(reader,'pressure_rows',return_value=rows):
   vals=reader.pressure_means(now,now+timedelta(hours=1))
   self.assertTrue(all(math.isnan(v) for _,v in vals[:3]))
   self.assertEqual(vals[-1][1],1005)
 def test_historical_gaps_serialize_but_are_not_drawn_as_forecasts(self):
  strip=ForecastStrip.model_construct(pressure_history=[1004,1005,None,1006,1007],pressure_history_times=['00:00']*5,pressure_history_timestamps=[],slots=[None]*7)
  JSONResponse({'pressure_history':strip.pressure_history})
  lines=[];_draw_pressure_graph(lines,strip)
  self.assertEqual(sum('<polyline ' in l for l in lines),0)
  self.assertTrue(any('Pressure outlook unavailable' in l for l in lines))
  strip.pressure_history=[None,None];lines=[];_draw_pressure_graph(lines,strip);self.assertEqual(sum('<polyline ' in l for l in lines),0)
 def test_build_forecast_with_missing_history_bins(self):
  from forecast_state import RooftopSnapshot
  now=datetime.now(timezone.utc)
  history=pd.DataFrame({'_time':[now-timedelta(minutes=10),now-timedelta(minutes=5),now],'pressure':[1004.2,float('nan'),1004.1]})
  with patch.multiple(main,get_latest_rooftop=lambda:RooftopSnapshot(pressure_hpa=1004.1),get_recent_hours_data=lambda **kw:pd.DataFrame(),get_rainfall_series=lambda **kw:pd.DataFrame(),get_recent_pressure=lambda **kw:history,get_raw_metno_fields=lambda now:{}):
   strip=main._build_forecast()
  self.assertEqual(strip.pressure_history,[1004.2,None,1004.1])
  JSONResponse(strip.model_dump(mode='json'))
  from svg_renderer import render_forecast_strip
  self.assertNotIn('nan',render_forecast_strip(strip).lower())
 def test_cached_pressure_expires_at_ten_minutes(self):
  from forecast_state import RooftopSnapshot,DerivedFeatures
  now=datetime.now(timezone.utc)
  strip=ForecastStrip.model_construct(rooftop=RooftopSnapshot(pressure_hpa=1004,observation_times={'pressure':(now-timedelta(minutes=10)).isoformat()}),derived=DerivedFeatures(pressure_delta_3h=-1),slots=[])
  main._expire_pressure(strip,now)
  self.assertIsNone(strip.rooftop.pressure_hpa)
  self.assertIsNone(strip.derived.pressure_delta_3h)
 def test_unfinished_pressure_bucket_is_never_labelled_in_the_future(self):
  stop=datetime(2026,11,17,0,3,tzinfo=timezone.utc)
  rows=[Record(stop-timedelta(minutes=8),1004),Record(stop-timedelta(minutes=1),1005)]
  with patch.object(reader,'pressure_rows',return_value=rows):
   values=reader.pressure_means(stop-timedelta(hours=1),stop)
  self.assertTrue(all(stamp<=stop for stamp,_ in values))
  self.assertEqual(values[-1][0],pd.Timestamp(stop.replace(minute=0)))
 def test_source_query_is_explicit(self):
  with patch.object(reader,'query_flux',return_value=[]) as query:
   now=datetime.now(timezone.utc);reader.pressure_rows(now-timedelta(hours=1),now)
   flux=query.call_args.args[0]
   self.assertIn('r.topic == "pressure/telemetry"',flux);self.assertIn('r.station == "local-pressure"',flux)

if __name__=='__main__':unittest.main()
