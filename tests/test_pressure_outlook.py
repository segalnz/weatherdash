import sys,unittest,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'venvs/barometer'))
from forecast_state import ForecastStrip,ForecastSlot,RooftopSnapshot,AtmosphericState
from svg_renderer import render_forecast_strip,MARGIN,SLOT_W
NS={'s':'http://www.w3.org/2000/svg'}


def forecast():
    slots=[ForecastSlot(hour_offset=h,time_label=str(7+h)+':00',state=AtmosphericState.CLOUDY,state_label='Cloudy',icon='',
                        source='local-instruments' if h==0 else 'llm-runtime',forecast_pressure_hpa=None if h==0 else 1002+h/10,
                        baseline_pressure_hpa=1011+h/10) for h in range(7)]
    return ForecastStrip(generated_at='test',slots=slots,rooftop=RooftopSnapshot(pressure_hpa=1002),
                         pressure_history=[999,None,1000],pressure_history_times=['01:00','02:00','03:00'],
                         pressure_history_timestamps=['2026-11-17T01:00:00Z','2026-11-17T02:00:00Z','2026-11-17T03:00:00Z'])


class PressureOutlookTests(unittest.TestCase):
    def test_chart_uses_forecast_not_sensor_history_and_aligns_with_slots(self):
        root=ET.fromstring(render_forecast_strip(forecast()))
        curves=root.findall('s:polyline[@data-role="pressure-outlook"]',NS)
        self.assertEqual(len(curves),1)
        points=root.findall('s:circle[@data-role="pressure-forecast-point"]',NS)
        self.assertEqual(len(points),7)
        self.assertEqual([float(p.attrib['data-value']) for p in points],[1002,1002.1,1002.2,1002.3,1002.4,1002.5,1002.6])
        self.assertEqual(points[0].attrib['data-source'],'observed')
        self.assertTrue(all(p.attrib['data-source']=='llm-runtime' for p in points[1:]))
        self.assertEqual([float(p.attrib['cx']) for p in points],[MARGIN+h*SLOT_W+(SLOT_W-10)/2 for h in range(7)])

    def test_unavailable_pressure_not_replaced_by_history_or_met(self):
        data=forecast()
        for slot in data.slots[1:]:slot.forecast_pressure_hpa=None
        svg=render_forecast_strip(data)
        root=ET.fromstring(svg)
        self.assertIn('Pressure outlook unavailable',svg)
        self.assertEqual(root.findall('s:polyline[@data-role="pressure-outlook"]',NS),[])
        self.assertNotIn('data-value="1011',svg)
        self.assertNotIn('data-value="999',svg)

    def test_met_reference_never_splices_observed_local_value(self):
        data=forecast()
        for slot in data.slots[1:]:slot.source='metno'
        root=ET.fromstring(render_forecast_strip(data))
        points=root.findall('s:circle[@data-role="pressure-forecast-point"]',NS)
        self.assertEqual(float(points[0].attrib['data-value']),1011)
        self.assertTrue(all(p.attrib['data-source']=='metno-reference' for p in points))

    def test_legacy_partial_forecast_is_explicitly_unavailable_not_joined(self):
        data=forecast();data.slots[3].forecast_pressure_hpa=None
        root=ET.fromstring(render_forecast_strip(data))
        self.assertEqual(len(root.findall('s:polyline[@data-role="pressure-outlook"]',NS)),2)
        self.assertEqual(len(root.findall('s:text[@data-role="pressure-unavailable"]',NS)),1)

if __name__=='__main__':unittest.main()
