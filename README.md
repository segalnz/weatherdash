# Weatherdash

Weatherdash combines local **temperature, humidity, wind speed and direction,
gusts, rainfall and barometric pressure** with MET Norway forecasts to produce a
rolling six-hour local outlook. It interprets how these observations change together,
checks their quality, and compares the resulting local evidence with the regional
forecast. Pressure is one part of that evidence, alongside moisture, temperature,
wind and measured rain.

The local instruments include an SHT45 temperature/humidity sensor, an anemometer
for wind and gusts, a Davis rain bucket, and a BMP388 pressure sensor connected
through LoRaWAN and ChirpStack. Light-level observations can also be ingested;
solar-normalized interpretation of lux remains a planned improvement. Each source
contributes only when suitable data is available—missing readings remain unknown.

The application runs on a Linux host or Proxmox LXC, stores observations and
forecasts in InfluxDB v2, and serves a FastAPI dashboard, JSON API and SVG forecast
strip. Grafana dashboard exports are also included. Sensor-specific pressure
compensation and correction happen in firmware, before Weatherdash receives the
corrected hPa readings; the application does not apply those corrections again.

## Forecasting approach: reason from evidence, without a local training prerequisite

Weatherdash is intended to interpret observations in the manner of an experienced
weather observer: assess the instruments, look at how conditions are changing,
consider the regional forecast, and explain the likely sequence over the next six
hours. A **large language model (LLM) assists in developing and refining the
meteorological rules**: helping articulate relationships between observations,
identify alternative explanations and translate that reasoning into explicit,
inspectable code. The core uses those rules with current evidence, rather than
fitting a site-specific predictor to months of labelled local weather outcomes.

In the current implementation, the LLM contributes during design and development;
it is not called for each forecast. The deployed engine applies the resulting
rules deterministically and produces its outlook without a runtime LLM dependency.
LLM-assisted rule development does not establish meteorological correctness or
forecast skill: the rules remain hypotheses to test against observations and the
MET baseline.

This makes it useful before a large local archive exists. Individual diagnostics
still need enough recent observations: for example, a three-hour pressure trend
requires suitable coverage over that period. Longer archives remain essential for
checking forecast skill and improving rules. Some ML approaches can use pretrained
models without months of site-specific data; Weatherdash's distinction is that
**local model training is not a prerequisite for its core reasoning**.

The intended process is:

1. **Check the evidence.** Verify sensor freshness, validity and coverage; treat
   missing readings as unknown. Distinguish atmospheric change from sensor faults.
2. **Interpret related changes together.** Falling pressure, moisture changes and
   sustained wind shifts may support a developing weather regime. Rising relative
   humidity caused by cooling is not an independent rain signal.
3. **Compare with MET Norway.** Use the cloud forecast as the regional baseline,
   then identify where local observations support it, differ from it or are
   insufficient to judge it. Separate agreement from independent corroboration.
4. **Make bounded, traceable adjustments.** Give observations more influence near
   the present and let unsupported local persistence decay with lead time. A
   temperature adjustment does not justify inventing a rain onset or probability.
5. **Explain one consolidated outlook.** Describe the likely evolution, important
   alternatives and uncertainty, backed by hourly details and an audit trail.
6. **Verify as data accumulates.** Compare issued forecasts with later observations
   and with MET alone, so plausible-sounding adjustments must demonstrate value.

For example, falling pressure and a rising dew point may support deterioration
already forecast by MET. They do not locate an incoming rain band or establish its
arrival time. Likewise, high humidity alone cannot confirm rain or fog.

**Implemented today:** sensor quality checks, time-aware trends, a structured
local evidence ledger, cautious regime candidates, explicit mixed local/MET
signals, and a consolidated outlook with timed MET wet/dry windows and limitations.
The API exposes this as `synthesis`; the browser dashboard displays it below the
forecast strip. Evidence dependencies prevent related pressure changes being counted
as independent corroboration. The outlook expires when its supporting observations
age out. Regime support is qualitative, not a calibrated probability.

Observation-based current conditions and MET hourly future forecasts remain the
baseline, with the existing bounded temperature adjustment. Future values are not
changed by the new narrative layer. Issue-time archives now include the synthesis,
local observation/feature snapshot and explicit model wind/rain baselines.

Regime assessments now have restart-safe persistence and asymmetric entry/exit
rules. A pattern needs at least ten minutes and three fresh observation cycles to
be called persistent; this describes repeated evidence, not a confirmed front or
fog event. Invalid evidence is withheld immediately. The previous successful
forecast cache carries the memory, so dashboard reads cannot advance it.

Verification now compares issue-time Weatherdash, MET and eligible persistence
baselines on the same observations. It reports paired errors by lead time and
persistent-regime context, plus complete-hour rain detection counts. The dashboard
provides a comparison table, and `GET /api/verification` exposes the scheduled
report. Missing baselines are never recreated from later observations. Read the
[persistence and verification guide](docs/regimes-and-verification.md).

**Proposed next:** evaluate the new records before changing local corrections;
improve the older confidence display and consider independent regional/event
observations. No calibrated probabilities or confirmed front/fog verification are
claimed. Beyond its current rule-development role, an LLM could optionally help
word future outlooks; runtime LLM synthesis is not part of the deployed engine.
See the [design review](docs/forecast-design-review.md).

The approach fits observation-aware short-range forecasting; WMO describes
[nowcasting and very-short-range forecasting](https://space.oscar.wmo.int/applicationareas/view/2_3_nowcasting_very_short_range_forecasting)
in this context. MET's [time and interval semantics](https://docs.api.met.no/doc/ForecastJSON)
remain authoritative for interpreting its hourly guidance.

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

The regression suites cover both the original forecast path and structured synthesis scenarios. Coverage includes forecast timing,
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
