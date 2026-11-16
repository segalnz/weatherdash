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

Weatherdash's core is a **runtime large language model (LLM) interpreter**.
It receives recent local sensor histories, data-quality flags, derived trends and
MET Norway guidance, then reasons about their combined meaning in the manner of an
experienced weather observer. Its output should explain the likely next six hours,
the evidence behind that interpretation, important alternatives and uncertainty.

Deterministic code prepares the evidence and checks the LLM's response. Incorrect
facts or unsupported inferences should be returned to the LLM for correction;
advisory disagreements should invite justification. The deterministic forecast is
not silently substituted for the LLM's interpretation. If bounded revision cannot
resolve a problem, that failure or disagreement must remain visible.

**Implementation status:** the scheduled forecast worker now calls DeepSeek directly
for runtime interpretation, validates its structured response, and returns specific
problems to the model for revision. It allows at most two revisions. Hard factual,
time, schema and kiosk-size problems must be corrected. Advisory disagreements may
be justified with input evidence and remain visible in the published explanation.
The deterministic forecast is not silently substituted if interpretation fails.

The intended approach does not require first training a new site-specific model. Individual diagnostics
still need enough recent observations: for example, a three-hour pressure trend
requires suitable coverage over that period. Longer archives remain essential for
checking forecast skill and improving rules. Some ML approaches can use pretrained
models without months of site-specific data; Weatherdash's distinction is that
**local model training is not a prerequisite for its core reasoning**.

The runtime process is:

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

**Implemented today:** sensor quality checks, time-aware histories/trends, a
structured evidence ledger, persistent advisory regime diagnostics, runtime LLM
interpretation with a challenge/revision loop, kiosk-sized rationale, audit records
and issue-time forecast verification. The LLM receives local observations, recent
histories, quality flags and timed MET data—not just a deterministic forecast to
reword. Its validated hourly conditions, temperature, wind and rainfall become
the future forecast; current numeric observations remain instrument readings.

MET and persistence forecasts remain explicit evaluation baselines. Deterministic
regimes are labelled sanity-check diagnostics, not asserted to be the LLM's own
conclusions. Missing inputs remain unknown. Local/MET disagreements and justified
departures remain visible. The API exposes the interpretation through `synthesis`
and top-level `interpreter_*` provenance fields.

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

**Proposed next:** evaluate issued LLM forecasts and correction exchanges against
the baselines before making skill claims; add independent regional/event evidence
and solar-normalized lux where available. Confidence is qualitative support, not a
calibrated probability. Read the [runtime interpreter guide](docs/runtime-llm.md)
and [design history](docs/forecast-design-review.md).

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
                                     runtime LLM ↔ sanity checks / revision
                                                               ↓
                                          cached forecast and verification
                                                               ↓
                                               FastAPI / SVG / Grafana
```

Local observations inform current conditions and pressure trends. Future slots use
LLM interpretations grounded in fresh, correctly timed MET predictions and local
evidence. Pressure and humidity alone do not establish precise rain arrival.
Missing or invalid evidence stays unknown. LLM support labels are qualitative,
not calibrated probabilities.

## Seeing the forecast basis

The six-hour SVG strip displays wrapped **Outlook** and **Basis** text beneath the
hourly slots, plus an important limitation when present. The footer grows modestly
when needed; longer explanations remain in the dashboard panel and the SVG's
accessible description. A stale explanation is withheld rather than shown as current.

The LLM supplies `synthesis.strip_explanation`, a concise account of the forecast
basis. The validator requests a shorter revision if its outlook, rationale or key
uncertainty cannot fit the fixed kiosk area. The renderer does not silently trim a
validated LLM explanation, and it never relies on scrolling, hover or click.

## Repository layout

| Path | Purpose |
| --- | --- |
| `venvs/barometer/` | Runtime LLM, evidence/quality, correction checks, rendering and archive |
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
INFLUX_TOKEN=offline-test LLM_ENABLED=false .venv/bin/python -m unittest discover -s tests -v
INFLUX_TOKEN=offline-test LLM_ENABLED=false .venv/bin/python -m unittest discover -s pressure-monitor/tests -v
```

The regression suites cover both the original forecast path and structured synthesis scenarios. Coverage includes forecast timing,
zero-value preservation, rain evidence, humidity quality, pressure freshness,
invalid readings, history gaps, JSON/SVG output and durable deduplication.

## Configuration and project status

This repository was assembled from the running Weatherdash source on 15 November
2026. Repository copies use environment configuration in place of embedded deployment
credentials; creating the repository does not change the running installation.
Deployment files are templates to review for your host, not an automatic installer.
The legacy Zambretti refinement caller is disabled. The active runtime interpreter
is `venvs/barometer/runtime_llm.py`; configure it as described in the runtime guide.
Repository examples default `LLM_ENABLED=false` to avoid unintended provider calls.
The deployed worker enables it explicitly with private service configuration.

Credentials, access tokens, Python environments, caches, logs, databases and backups
are excluded. Public visibility does not grant an open-source license; no license
has been selected for this repository.
