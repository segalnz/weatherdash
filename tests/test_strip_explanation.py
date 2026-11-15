import sys,unittest
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'venvs/barometer'))
from forecast_state import ForecastStrip,ForecastSlot,AtmosphericState,DerivedFeatures
from synthesis import synthesise
from test_synthesis import snapshot,model,NOW
from svg_renderer import render_forecast_strip,OFFSET_Y,SLOT_H
NS={'s':'http://www.w3.org/2000/svg'}


def strip():
    outlook=synthesise(snapshot(),DerivedFeatures(pressure_delta_3h=-2,dewpoint_delta_1h=1),model(),NOW)
    return ForecastStrip(generated_at='test',synthesis=outlook,slots=[ForecastSlot(hour_offset=h,state=AtmosphericState.CLOUDY,state_label='Cloudy',icon='') for h in range(7)])


class StripExplanationTests(unittest.TestCase):
    def test_visible_basis_contains_actual_evidence_and_model_source(self):
        root=ET.fromstring(render_forecast_strip(strip()))
        rows=root.findall('s:text[@data-role="forecast-explanation"]',NS)
        visible=' '.join(r.text for r in rows)
        self.assertIn('Outlook:',visible)
        self.assertIn('Basis:',visible)
        self.assertIn('MET Norway',visible)
        self.assertIn('pressure -2.0 hPa/3h',visible)
        self.assertIn('dew point rising',visible)
        self.assertIn('Note:',visible)

    def test_long_explanations_are_bounded_and_do_not_overlap_slots(self):
        data=strip();data.synthesis.summary='A long forecast explanation with supporting observations. '*100
        data.synthesis.strip_explanation='Detailed supporting evidence and model reconciliation. '*100
        root=ET.fromstring(render_forecast_strip(data))
        rows=root.findall('s:text[@data-role="forecast-explanation"]',NS)
        self.assertLessEqual(len(rows),5)
        for row in rows:
            self.assertGreater(float(row.attrib['y']),OFFSET_Y+SLOT_H)
            self.assertLess(float(row.attrib['y']),float(root.attrib['height'])-20)
            self.assertLessEqual(len(row.text),140)
        self.assertLessEqual(float(root.attrib['height']),624)
        description=root.find('s:desc',NS).text
        self.assertIn(data.synthesis.summary,description)
        self.assertIn(data.synthesis.strip_explanation,description)

    def test_supplied_interpreter_explanation_takes_precedence_and_is_escaped(self):
        data=strip();data.synthesis.strip_explanation='Wind shift & rising moisture support <possible deterioration>; rain timing remains uncertain.'
        rendered=render_forecast_strip(data);root=ET.fromstring(rendered)
        visible=' '.join(r.text for r in root.findall('s:text[@data-role="forecast-explanation"]',NS))
        self.assertIn('Wind shift & rising moisture',visible)
        self.assertNotIn('pressure -2.0',visible)
        self.assertIn('&lt;possible deterioration&gt;',rendered)
        self.assertEqual(root.findall('.//s:script',NS),[])

    def test_missing_or_expired_synthesis_does_not_show_old_facts(self):
        data=strip();data.synthesis=None;data.evidence=['Old pressure fact must not be presented as current']
        rendered=render_forecast_strip(data)
        self.assertIn('awaiting a fresh outlook',rendered)
        self.assertNotIn(data.evidence[0],rendered)

if __name__=='__main__':unittest.main()
