import base64
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bridge', ROOT/'tools/pressure_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
DEVICE = '0123456789abcdef'
NOW = 1794690000.0


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.event=json.loads((ROOT.parent / 'deploy/example-chirpstack-uplink.json').read_text())
        self.event['time']=datetime.fromtimestamp(NOW, timezone.utc).isoformat()

    def normalize(self, event=None, now=NOW):
        return bridge.normalize(self.event if event is None else event, DEVICE, now)

    def test_valid_vector_no_second_correction(self):
        packet=self.normalize()
        self.assertEqual(packet['pressure'],1032.13)
        self.assertEqual(packet['schema_version'],3)
        self.assertEqual(packet['sample_time'],NOW)
        self.assertFalse({'pressure_station_hpa','sensor_temperature_c'} & packet.keys())

    def test_invalid_vector_has_no_pressure(self):
        self.event['data']=base64.b64encode(bytes.fromhex('4404d30000000000')).decode()
        obj=self.event['object']; obj.update(sequence=1235,quality_valid=False,sensor_status='init_failed')
        obj.pop('pressure_hpa');obj.pop('sensor')
        packet=self.normalize()
        self.assertIs(packet['quality_valid'],False)
        self.assertNotIn('pressure',packet)

    def test_stale_future_and_boundary(self):
        for now in (NOW+600,NOW-31):
            with self.subTest(now=now),self.assertRaises(ValueError): self.normalize(now=now)
        self.normalize(now=NOW+599)
        self.normalize(now=NOW-30)

    def test_reject_wrong_identity_schema_port_and_pressure(self):
        for change in ({'deviceInfo':{'devEui':'0000000000000000'}}, {'fPort':0}, {'object':{}}):
            with self.subTest(change=change),self.assertRaises(ValueError): self.normalize(self.event|change)
        for key,val in [('pressure_hpa',float('nan')),('pressure_hpa',0),('schema_version',2),('quality_valid',1),('correction_location','bridge')]:
            e=deepcopy(self.event);e['object'][key]=val
            with self.subTest(key=key),self.assertRaises(ValueError): self.normalize(e)

    def test_actual_chirpstack_numeric_types_and_age(self):
        self.event['object'].update(sequence=3.0,sample_age_seconds=45.0,schema_version=3.0)
        self.assertEqual(self.normalize()['sample_time'],NOW-45)
        self.assertEqual(self.normalize()['sequence'],3)
        self.event['object']['sequence']=3.5
        with self.assertRaises(ValueError):self.normalize()

    def test_outbox_survives_restart_and_suppresses_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.sqlite'
            box=bridge.Outbox(path);packet=self.normalize()
            self.assertTrue(box.enqueue(packet));self.assertFalse(box.enqueue(packet))
            box.db.close();box=bridge.Outbox(path)
            self.assertEqual(json.loads(box.next()[2])['sample_time'],NOW)
            self.assertFalse(box.enqueue(packet|{'event_id':'different'}))
            box.complete(packet['event_id']);self.assertIsNone(box.next())
            # Sequence wraps/reboots do not affect event ordering.
            self.assertTrue(box.enqueue(packet|{'event_id':'new','sample_time':NOW+300,'sequence':0}))
            self.assertFalse(box.enqueue(packet|{'event_id':'older','sample_time':NOW-1}))
            box.db.close()


if __name__=='__main__': unittest.main()
