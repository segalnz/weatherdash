# Deployment and pressure contract

Review the templates under `deploy/` before installation. They assume a checkout
at `/opt/weatherdash`, a shared `.venv`, a `weatherdash` account for weather jobs,
and a dedicated `pressure-bridge` account for the bridge. Provision those accounts
and InfluxDB/Telegraf independently; no host bootstrap is performed by this repo.

The weather services read `/etc/weatherdash.env`. The bridge reads the private
`/etc/pressure-bridge.env`. systemd reads these EnvironmentFiles before switching
users; keep them root-owned and mode 0600. Copy only the needed keys from
`.env.example` and `deploy/pressure-bridge.env.example`. Do not install placeholders
as real credentials. Set an identifying contact in `METNO_USER_AGENT`, plus your
location and elevation. The API itself has no authentication; expose it only on a
trusted network or behind an appropriately configured proxy.

## InfluxDB and sources

The default organization is `weather`. Buckets used by the source are
`weatherdata`, `weather_forecast` and `forecast_derived`. Provision them and suitable
retention policies/access tokens before running the ingesters.

Canonical corrected pressure uses bucket `weatherdata`, measurement `mqtt_consumer`,
topic tag `pressure/telemetry`, station tag `local-pressure`, field `pressure` in hPa.
Latest values, five-minute trend/history bins and verification use that same source.
The old CSV input must exclude `pressure` alongside its temperature/humidity/dewpoint
exclusions. Preserve all seven CSV column positions and speed/gust/direction fields.
Do not stop a shared wind controller just to retire its pressure sensor.

`deploy/telegraf-pressure.conf.example` is an input/processor fragment, not a complete
Telegraf configuration. Merge it with the existing Influx output and other sensor
inputs. Supply `MQTT_HOST`, `MQTT_PORT`, `PRESSURE_MQTT_USERNAME` and
`PRESSURE_MQTT_PASSWORD` to the **Telegraf service**, not only to the bridge.
The sample-age field is explicitly float to match the existing shared measurement.

SHT45 temperature/humidity use topic `sht45/telemetry`; wind uses
`status/weather/data`. Those existing Telegraf inputs and their quality gates are
not reproduced by the pressure-only configuration fragment. Rainfall has its own
aggregation service. All required ingestion paths must be configured on a new host.

## Decoded pressure schema

The ChirpStack `object` must include schema_version 3, corrected `pressure_hpa`,
quality_valid, sensor_status, sequence, sample_age_seconds, pressure_basis
`legacy_sea_level_corrected`, and correction_location `firmware`. Sequence and age
can arrive as integer-valued JSON decimals. Invalid events omit numeric pressure.
The node's correction assumptions remain firmware-owned; Weatherdash does not
apply another altitude, temperature or calibration adjustment.

Normalized packets use `pressure` instead of `pressure_hpa`, include identity and
sample_time, and omit numeric pressure on invalid events. Sample time is ChirpStack
UTC event time minus sample age. Reject observations at least ten minutes old or
more than thirty seconds in the future. No temperature or raw station pressure is
required. Example packets in `deploy/` are illustrative, not live telemetry.

The bridge's SQLite StateDirectory preserves deduplication, a timestamp high-water
mark and pending publications. Run one instance and preserve its state over restarts.
Retries retain original sample timestamps. Newer invalid readings mask older valid
readings. History gaps become JSON nulls and separate SVG line segments; they are
never filled from the old pressure source. Allow one/three hours of new-source
coverage before expecting those trends, and six hours for a full history chart.

## Services and checks

Templates include the API, scheduled forecast/archive, MET ingestion, verification,
rain aggregation and pressure bridge. Adjust paths and account permissions. The API
and scheduled forecast must share `FORECAST_CACHE`; MET ingestion needs writable
`METNO_CACHE`. Configure service state directories accordingly. Do not enable a
service until its credentials and data dependencies are present.

Confirm a real reading through ChirpStack → bridge → Telegraf → Influx → forecast
cache → `/api/forecast` and `/api/forecast/svg`. Also verify existing wind, humidity,
rain and cloud-forecast ingestion. Execute the exported Grafana queries against your
InfluxDB; import only the dashboards needed and configure their data source.

Back up all affected configs and source before a canonical-source change. Rollback
must restore ingestion and consumers together to a single source. Preserve historical
Influx data and bridge state; do not delete data or introduce mixed-source fallback.
