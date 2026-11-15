#!./bin/python3
"""
rainfall.py — Davis rain bucket MQTT → InfluxDB v2
Subscribes to esp32/davis/rainfall, accumulates 2-min slot tips,
writes 10-min aggregates to InfluxDB on a fixed clock schedule.

Payload format: "slot:tips"  e.g. "14:3" = slot 14, 3 tips = 0.3mm
Slots 0-29 per hour, reset each hour.
10-min windows: slots 0-4, 5-9, 10-14, 15-19, 20-24, 25-29
Missing slots written as NaN for later ML gap identification.
"""

import os
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from datetime import datetime, timezone, timedelta
import threading
import logging
import math
import sys

# ── Config ────────────────────────────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT     = 1883
MQTT_TOPIC    = "esp32/davis/rainfall"
MQTT_USER = os.environ["MQTT_USERNAME"]
MQTT_PASS = os.environ["MQTT_PASSWORD"]
MQTT_CLIENT   = "rainfall-influx"

INFLUX_URL = os.getenv("INFLUX_URL", "http://127.0.0.1:8086")
INFLUX_TOKEN  = os.environ["INFLUX_TOKEN"]
INFLUX_ORG = os.getenv("INFLUX_ORG", "weather")
INFLUX_BUCKET = "weatherdata"

MM_PER_TIP    = 0.1   # Davis standard bucket calibration
SLOTS_PER_WIN = 5     # 5 × 2-min slots = 10-min window

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────
# slots[slot_number] = tip_count  (populated as MQTT messages arrive)
slots: dict[int, int] = {}
slots_lock = threading.Lock()

# ── InfluxDB ──────────────────────────────────────────────────────────────────
client     = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api  = client.write_api(write_options=SYNCHRONOUS)

# ── Helpers ───────────────────────────────────────────────────────────────────

def window_for_slot(slot: int) -> int:
    """Return 10-min window index (0-5) for a given slot (0-29)."""
    return slot // SLOTS_PER_WIN

def window_start_time(window: int, ref: datetime) -> datetime:
    """Return UTC timestamp for the start of a 10-min window within ref's hour."""
    return ref.replace(minute=window * 10, second=0, microsecond=0)

def current_window(now: datetime) -> int:
    """Which 10-min window are we currently in."""
    return now.minute // 10

def write_window(window: int, window_time: datetime, slot_data: dict):
    """
    Write a 10-min aggregate to InfluxDB.
    slot_data: dict of slot→tips for slots in this window.
    Missing slots recorded as NaN.
    """
    expected_slots = list(range(window * SLOTS_PER_WIN, (window + 1) * SLOTS_PER_WIN))
    total_tips = 0
    missing = []
    present = []

    for s in expected_slots:
        if s in slot_data:
            total_tips += slot_data[s]
            present.append(s)
        else:
            missing.append(s)

    if missing:
        rainfall_mm = float('nan')
        log.warning(f"Window {window} ({window_time.strftime('%H:%M')}): "
                    f"missing slots {missing}, writing NaN")
    else:
        rainfall_mm = total_tips * MM_PER_TIP
        log.info(f"Window {window} ({window_time.strftime('%H:%M')}): "
                 f"{rainfall_mm:.1f}mm ({total_tips} tips, slots {present})")

    point = (
        Point("rainfall")
        .tag("station", "davis-rainbucket")
        .tag("location", "575 Moonshine Hill Road")
        .field("rainfall_mm", rainfall_mm)
        .field("tip_count", total_tips if not missing else -1)  # -1 signals incomplete
        .field("window", window)
        .field("slots_received", len(present))
        .field("slots_missing", len(missing))
        .time(window_time, write_precision="s")
    )

    try:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
    except Exception as e:
        log.error(f"InfluxDB write failed: {e}")

# ── Scheduler ─────────────────────────────────────────────────────────────────

class WindowScheduler:
    """Fires at the end of each 10-min window aligned to the clock."""

    def __init__(self):
        self.last_window_written = -1
        self._schedule_next()

    def _schedule_next(self):
        now = datetime.now(timezone.utc)
        # Next 10-min boundary
        next_min = ((now.minute // 10) + 1) * 10
        if next_min >= 60:
            next_dt = (now + timedelta(hours=1)).replace(
                minute=0, second=2, microsecond=0)
        else:
            next_dt = now.replace(minute=next_min, second=2, microsecond=0)

        delay = (next_dt - now).total_seconds()
        log.info(f"Next window write in {delay:.0f}s at {next_dt.strftime('%H:%M:%S')} UTC")
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        now = datetime.now(timezone.utc)
        # The window that just completed
        completed_window = (now.minute // 10) - 1
        if completed_window < 0:
            completed_window = 5  # wrapped — last window of previous hour
            window_time = (now - timedelta(hours=1)).replace(
                minute=50, second=0, microsecond=0)
        else:
            window_time = window_start_time(completed_window, now)

        with slots_lock:
            # Extract only slots belonging to the completed window
            slot_data = {
                s: t for s, t in slots.items()
                if window_for_slot(s) == completed_window
            }
            # Clear written slots
            for s in list(slots.keys()):
                if window_for_slot(s) == completed_window:
                    del slots[s]

        write_window(completed_window, window_time, slot_data)
        self._schedule_next()

    def stop(self):
        self._timer.cancel()

# ── MQTT ──────────────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        log.info(f"Connected to MQTT broker {MQTT_BROKER}")
        client.subscribe(MQTT_TOPIC, qos=1)
        log.info(f"Subscribed to {MQTT_TOPIC}")
    else:
        log.error(f"MQTT connect failed, reason code: {reason_code}")

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode().strip()
        parts = payload.split(":")
        if len(parts) != 2:
            log.warning(f"Unexpected payload format: {payload!r}")
            return

        slot = int(parts[0])
        tips = int(parts[1])

        if not (0 <= slot <= 29):
            log.warning(f"Slot {slot} out of range, ignoring")
            return

        with slots_lock:
            if slot in slots:
                log.warning(f"Slot {slot} already received this hour, ignoring duplicate")
                return
            slots[slot] = tips

        log.debug(f"Slot {slot}: {tips} tips ({tips * MM_PER_TIP:.1f}mm)")

    except Exception as e:
        log.error(f"Error processing message: {e}, payload: {msg.payload!r}")

def on_disconnect(client, userdata, flags, reason_code, properties=None):
    log.warning(f"MQTT disconnected, reason code: {reason_code}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Rainfall service starting")

    scheduler = WindowScheduler()

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT)
    mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
    mqttc.on_connect    = on_connect
    mqttc.on_message    = on_message
    mqttc.on_disconnect = on_disconnect

    mqttc.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

    try:
        mqttc.loop_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        scheduler.stop()
        mqttc.disconnect()
        client.close()

if __name__ == "__main__":
    main()
