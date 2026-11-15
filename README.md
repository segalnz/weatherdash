# Weatherdash

Weatherdash combines local weather-station observations with MET Norway forecasts
to produce a rolling six-hour local forecast. It runs on a Linux host or Proxmox
LXC, stores observations and forecasts in InfluxDB v2, and serves a FastAPI dashboard,
JSON API and SVG forecast strip. Grafana dashboard exports are also included.

The current pressure source is a BMP388 LoRaWAN node routed through ChirpStack.
Firmware performs pressure compensation and correction. Weatherdash accepts the
already-corrected pressure in hPa; it does not apply those corrections again.

## Data flow

```text
Pressure node → LoRaWAN → ChirpStack decoded MQTT uplink → pressure bridge
                                                       ↓
                                               pressure/telemetry
                                                       ↓
Wind / SHT45 / rain → MQTT → Telegraf or dedicated ingesters → InfluxDB
                                                               ↑
                                                MET Norway forecast ingester
                                                               ↓
                                      scheduled local forecast and verification
                                                               ↓
                                               FastAPI / SVG / Grafana
```

Local observations inform current conditions and pressure trends. Future slots use
fresh, correctly timed MET predictions. Pressure and humidity alone do not establish
rain or its arrival time. Missing or invalid evidence stays unknown. Confidence is
a heuristic display score, not a calibrated probability.

## Repository layout

| Path | Purpose |
| --- | --- |
| `venvs/barometer/` | Forecast service, sensor quality, trends, rendering and archive |
| `venvs/metno_forecast.py` | MET Norway forecast ingestion |
| `venvs/forecast_processor.py` | Verification against archived issued forecasts |
| `venvs/rainfall/`, `venvs/wind/` | Rain aggregation and prevailing wind processing |
| `pressure-monitor/tools/pressure_bridge.py` | Generic decoded-pressure MQTT bridge |
| `deploy/` | systemd/Telegraf examples and illustrative MQTT packets |
| `dashboards/` | Grafana dashboard exports |
| `tests/`, `pressure-monitor/tests/` | Offline regression tests |

`venvs/` is the historical source-directory name. It contains source files only in
this repository; create your Python environment in `.venv/`.

## Development

Use Python 3.12 or newer. The deployed installation was tested with Python 3.14.

```sh
git clone https://github.com/segalnz/weatherdash.git
cd weatherdash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Set the InfluxDB token, MQTT credentials, location and MET contact user agent in
`.env`. The programs read process environment variables; they do not automatically
load dotenv files. For local development:

```sh
set -a
. ./.env
set +a
.venv/bin/uvicorn main:app --app-dir venvs/barometer --host 127.0.0.1 --port 8090
```

The web service reads a forecast cache. Starting the API alone does not create
forecasts. With InfluxDB configured and sensor/forecast data available, run the
scheduled pipeline or generate a development snapshot:

```sh
.venv/bin/python venvs/metno_forecast.py
FORECAST_CACHE="$PWD/forecast_cache.json" .venv/bin/python venvs/barometer/snapshot_forecast.py
FORECAST_CACHE="$PWD/forecast_cache.json" .venv/bin/uvicorn main:app --app-dir venvs/barometer --port 8090
```

The API is `GET /api/forecast`; the SVG strip is `GET /api/forecast/svg`; the
browser dashboard is `/`. A missing/stale forecast cache produces unavailable
conditions rather than invented readings.

## Pressure integration

The bridge subscribes to one exact ChirpStack application/device uplink topic:

```text
application/<APPLICATION_ID>/device/<DEV_EUI>/event/up
```

It consumes decoded schema 3 on FPort 30 and publishes `pressure/telemetry` with
QoS 1 and retain disabled. Configure broker credentials, application ID and DevEUI
through environment variables. ChirpStack decoding and board firmware are maintained
separately; the bridge has no chip-register or compensation code.

Read [the deployment guide](docs/deployment.md) for the field contract, source
selection, service layout and operational checks.

## Tests

These regression tests use fixtures and mocks rather than publishing test readings
to a live broker. An arbitrary token satisfies configuration during offline tests:

```sh
INFLUX_TOKEN=offline-test .venv/bin/python -m unittest discover -s tests -v
INFLUX_TOKEN=offline-test .venv/bin/python -m unittest discover -s pressure-monitor/tests -v
```

The initial repository snapshot passes 25 tests. Coverage includes forecast timing,
zero-value preservation, rain evidence, humidity quality, pressure freshness,
invalid readings, history gaps, JSON/SVG output and durable deduplication.

## Configuration and project status

This repository was assembled from the running Weatherdash source on 15 November
2026. Repository copies use environment configuration in place of embedded deployment
credentials; creating the repository does not change the running installation.
Deployment files are templates to review for your host, not an automatic installer.
The optional legacy LLM module is retained for compatibility with its tests; it is
not the active forecast path. External LLM synthesis is not implemented here.

Credentials, access tokens, Python environments, caches, logs, databases and backups
are excluded. Public visibility does not grant an open-source license; no license
has been selected for this repository.
