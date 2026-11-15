"""Forward decoded ChirpStack pressure to MQTT, without sensor-specific correction."""
from datetime import datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time

LOG = logging.getLogger('pressure-bridge')
OUTPUT_TOPIC = 'pressure/telemetry'


def normalize(event, device_eui, now=None):
    now = time.time() if now is None else now
    if event.get('deviceInfo', {}).get('devEui', '').lower() != device_eui.lower():
        raise ValueError('wrong device')
    if event.get('fPort') != 30:
        raise ValueError('not a pressure FPort')
    obj = event.get('object')
    if not isinstance(obj, dict) or obj.get('schema_version') != 3:
        raise ValueError('expected decoded pressure schema 3')
    if obj.get('pressure_basis') != 'legacy_sea_level_corrected' or obj.get('correction_location') != 'firmware':
        raise ValueError('unexpected pressure basis/correction provenance')
    quality = obj.get('quality_valid')
    status = obj.get('sensor_status')
    if type(quality) is not bool or not isinstance(status, str) or not status:
        raise ValueError('missing quality/status')
    if quality != (status == 'ok'):
        raise ValueError('inconsistent quality/status')
    sequence, age = obj.get('sequence'), obj.get('sample_age_seconds')
    if type(sequence) not in (int, float) or not math.isfinite(sequence) or sequence != int(sequence) or not 0 <= sequence <= 65535:
        raise ValueError('invalid sequence')
    if type(age) not in (int, float) or not math.isfinite(age) or age != int(age) or not 0 <= age < 600:
        raise ValueError('invalid sample age')
    sequence, age = int(sequence), int(age)
    event_time = datetime.fromisoformat(event['time'].replace('Z', '+00:00'))
    if event_time.utcoffset() is None:
        raise ValueError('event time needs timezone')
    stamp = event_time.timestamp() - age
    if not math.isfinite(stamp) or not -30 <= now - stamp < 600:
        raise ValueError('stale/future observation')
    value = obj.get('pressure_hpa')
    if quality:
        if type(value) not in (int, float) or not math.isfinite(value) or not 0.01 <= value < 2000:
            raise ValueError('invalid corrected pressure')
    elif value is not None:
        raise ValueError('invalid packet includes pressure')
    expected = dict(schema_version=3, sequence=sequence, sample_age_seconds=age,
                    quality_valid=quality, sensor_status=status,
                    pressure_basis=obj['pressure_basis'], correction_location='firmware')
    for key in ('altitude_m', 'legacy_calibration_hpa'):
        if key in obj:
            number = obj[key]
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError('invalid correction metadata')
            expected[key] = number
    identity = event.get('deduplicationId')
    if not isinstance(identity, str) or not identity:
        identity = hashlib.sha256(json.dumps([device_eui.lower(), event['time'], event['data']],
                                             separators=(',', ':')).encode()).hexdigest()
    out = dict(expected, station='local-pressure', dev_eui=device_eui.lower(),
               event_id=identity, sample_time=stamp,
               time_source='chirpstack_event_minus_sample_age')
    if quality:
        out['pressure'] = float(value)
    return out


class Outbox:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, stamp REAL);
            INSERT OR IGNORE INTO state VALUES(1, 0);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, stamp REAL, payload TEXT, pending INTEGER);''')

    def enqueue(self, packet):
        with self.lock, self.db:
            stamp = packet['sample_time']
            if stamp <= self.db.execute('SELECT stamp FROM state WHERE id=1').fetchone()[0]:
                return False
            if self.db.execute('SELECT 1 FROM events WHERE id=?', (packet['event_id'],)).fetchone():
                return False
            self.db.execute('INSERT INTO events VALUES(?,?,?,1)',
                            (packet['event_id'], stamp, json.dumps(packet, allow_nan=False)))
            self.db.execute('UPDATE state SET stamp=? WHERE id=1', (stamp,))
            self.db.execute('DELETE FROM events WHERE stamp < ? AND pending=0', (stamp - 172800,))
            return True

    def next(self):
        with self.lock:
            return self.db.execute('SELECT id,stamp,payload FROM events WHERE pending=1 ORDER BY stamp LIMIT 1').fetchone()

    def complete(self, identity):
        with self.lock, self.db:
            self.db.execute('UPDATE events SET pending=0 WHERE id=?', (identity,))


def main():
    import paho.mqtt.client as mqtt
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    device = os.environ['DEVICE_EUI'].lower()
    application = os.environ['CHIRPSTACK_APPLICATION_ID']
    if not re.fullmatch('[0-9a-f]{16}', device) or not re.fullmatch('[A-Za-z0-9-]+', application):
        raise ValueError('invalid source identity configuration')
    topic = f'application/{application}/device/{device}/event/up'
    outbox = Outbox(os.getenv('PRESSURE_STATE_DB', '/var/lib/pressure-bridge/outbox.sqlite3'))
    stop = threading.Event()
    fatal = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='weatherdash-pressure-bridge',
                         clean_session=True, manual_ack=True)
    client.max_queued_messages_set(10)
    client.reconnect_delay_set(1, 60)
    client.username_pw_set(os.environ['MQTT_USERNAME'], os.environ['MQTT_PASSWORD'])

    def connected(c, userdata, flags, reason, properties):
        if reason.is_failure:
            LOG.error('MQTT connection rejected: %s', reason)
            return
        LOG.info('Connected; subscribing to %s', topic)
        c.subscribe(topic, qos=1)

    def subscribed(c, userdata, mid, codes, properties):
        if any(code.is_failure for code in codes):
            LOG.error('MQTT subscription rejected')
            fatal.set()
            stop.set()

    def message(c, userdata, msg):
        if msg.topic != topic:
            return
        try:
            if not msg.retain:
                if len(msg.payload) > 65536:
                    raise ValueError('oversized event')
                packet = normalize(json.loads(msg.payload), device)
                if outbox.enqueue(packet):
                    LOG.info('Queued pressure observation; valid=%s', packet['quality_valid'])
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            LOG.warning('Rejected uplink: %s', exc)
        except sqlite3.Error:
            LOG.exception('Could not persist event; stopping without acknowledging')
            fatal.set()
            stop.set()
            return
        if msg.qos:
            c.ack(msg.mid, msg.qos)

    client.on_connect = connected
    client.on_subscribe = subscribed
    client.on_message = message
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    client.connect_async(os.getenv('MQTT_HOST', 'localhost'), int(os.getenv('MQTT_PORT', '1883')), 60)
    client.loop_start()
    try:
        while not stop.wait(1):
            row = outbox.next()
            if row is None:
                continue
            identity, stamp, payload = row
            if time.time() - stamp >= 600:
                LOG.warning('Discarding expired pending observation')
                outbox.complete(identity)
                continue
            if not client.is_connected():
                continue
            info = client.publish(OUTPUT_TOPIC, payload, qos=1, retain=False)
            # Exit/restart on delivery failure: avoid accumulating in-memory retries.
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError('MQTT publish failed')
            info.wait_for_publish(timeout=15)
            if not info.is_published():
                raise RuntimeError('MQTT publication acknowledgement timed out')
            outbox.complete(identity)
            LOG.info('Published pressure observation')
    finally:
        client.disconnect()
        client.loop_stop()
    if fatal.is_set():
        raise RuntimeError('MQTT subscription or persistence failed')


if __name__ == '__main__':
    main()
