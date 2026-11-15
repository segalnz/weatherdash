# Regime persistence and verification

Implemented 16 November 2026. This increment tracks expert-rule hypotheses over
successive forecasts and evaluates numerical forecasts. It does not train a model,
change MET precipitation timing or claim that the rules outperform the baselines.

## Persistence policy

A **candidate** becomes **persistent** only after at least ten minutes and three
fresh qualifying observation cycles. Repeated packets, API reads and re-evaluation
of identical sensor timestamps do not count as new evidence. All sensors required
for a regime must advance before another cycle counts. These engineering defaults
need evaluation; “persistent” does not mean a meteorological event is confirmed.

| Regime | Entry conditions | Broader retention band |
| --- | --- | --- |
| Deterioration | Three-hour pressure change ≤ −1 hPa | Pressure change ≤ −0.5 hPa |
| Possible frontal passage | Three-hour pressure rise ≥1 hPa, one-hour rise ≥0.5 hPa, cooling ≥1°C/h, non-calm direction shift ≥45° | Pressure rises ≥0.5/0.2 hPa respectively, cooling ≥0.5°C/h, shift ≥30° |
| Mist/low-cloud potential | Valid humidity, temperature/dew-point spread ≤2°C and wind ≤8 km/h | Spread ≤3°C and wind ≤12 km/h |

Wind direction evidence still requires populated, non-calm history windows. A weak
or missing directional diagnostic is not evidence of a front. Humidity quality flags
prevent moisture/mist corroboration. The current rules only propose these regimes;
there is no independent observation confirming a front or fog.

When entry conditions weaken, a previously persistent hypothesis is labelled
**weakening**. Its previous support IDs are not presented as current evidence.
Outside the retention band, exit requires ten minutes and at least two fresh cycles.
Any weakening hypothesis expires after fifteen minutes without renewed entry support.
A candidate that loses its entry conditions is dropped instead of being retained.

Missing/invalid/stale required readings or missing trend coverage withhold the
assessment immediately. A forecast-update gap over fifteen minutes resets tracking;
observation-time regression also withholds it. Backwards forecast time is rejected.
A restart does not reset healthy progress: memory and transitions are serialized in
`synthesis.regime_memory` and `synthesis.regime_transitions` in the last successful
atomic forecast cache. The scheduled generator locks updates to avoid concurrent
writers. Failed archive/cache publication does not advance the successful state.
Older caches without regime memory are accepted and begin with candidates.

## Issue-time baselines

Each new archived forecast stores available MET baselines and eligible local
persistence values, including their original observation timestamps. Persistence
holds the issue-time observation constant; it is a benchmark, not a recommended
forecast adjustment. Direction persistence is withheld in calm wind. Missing or
quality-flagged issue-time values produce no persistence benchmark.

Historical forecasts without those values cannot gain a reconstructed baseline:
future observations must not leak into the issue-time forecast. Existing records
can still be checked against whatever baselines were actually archived.

## Matching observations

For instantaneous variables, use the newest matching-source observation **at or
before** valid time, strictly less than ten minutes old. A newer invalid packet
masks older good temperature/humidity/pressure. Conflicting duplicate-time truth
is excluded. Pressure must use the canonical topic, station and corrected basis.
Wind direction errors are circular and withheld below 8 km/h; speed zero remains
a real value. This matching policy is versioned as `verification_v2`.

Rain amounts refer to [valid time, valid time + 1 hour). Verification waits until
that hour has completed, plus a five-minute ingestion grace. All six ten-minute
Davis gauge bins must exist with no missing slots, finite nonnegative amounts,
correct timestamps and no duplicates. Missing gauge data is not dry weather.
The occurrence threshold is **0.1 mm per hour**, reflecting the existing 0.1 mm
bucket increment. This verification threshold does not change forecast labels.

Rain is compared with MET amounts and an explicit always-dry reference. No rain
persistence benchmark is invented from a single recent interval. Pressure errors
also reflect the configured legacy pressure reduction/calibration basis; they do
not isolate forecast-model error alone.

## Report and dashboard

The scheduled forecast processor recomputes the previous 72 hours by default:

```sh
INFLUX_TOKEN=... .venv/bin/python venvs/forecast_processor.py --hours 72
```

For a read-only recomputation that prints JSON and writes neither Influx nor files:

```sh
INFLUX_TOKEN=... .venv/bin/python venvs/forecast_processor.py --hours 24 --report-only
```

For each model version and actual lead-hour bucket, the report gives bias, MAE,
RMSE and counts by variable/method. Signed error/bias is observation minus forecast. `paired` comparisons use exactly the same
observations for Weatherdash and each reference; positive MAE improvement means
lower error than that reference. They do not compare averages from unmatched samples.
Lead hour 1 means (0,1] hours from issue to valid time, not exactly one hour.

The report includes overall cohorts and persistent-regime subcohorts; an empty
regime list means no persistent assessment, not established stable weather. Missing
historical regime metadata is labelled `unrecorded`. Subcohorts can overlap and
must not be added. Rain occurrence provides hits, misses, false alarms and correct
negatives, not fabricated probabilities or uncalibrated probability scores.

The atomic report file defaults to
`/var/lib/weatherdash/verification_report.json`, configurable with
`FORECAST_VERIFICATION_REPORT`. The scheduled processor must be able to write its
parent directory. The API reads the same path and exposes `GET /api/verification`;
it does not run verification on display requests. Reports over one hour old are
marked stale. The browser shows paired comparisons for the current rules and
explains when no mature pairs exist yet.

Detailed points use measurement `forecast_verification`, tagged with the policy
version, and per-variable `*_verified` flags. A false flag invalidates any residual
error fields from earlier processing of that point. Legacy `forecast_error` fields
remain for existing dashboards; consumers must respect the new eligibility flags
when inspecting reprocessed results. The fresh report is the authoritative paired
comparison view.

## What the results do and do not establish

Overlapping five-minute issue forecasts share observations and weather events;
the sample count is not a count of independent events. Small-sample improvements
are not proof of skill or statistical significance. Results are separated by rule
version and lead, and can be inspected by regime. Season-spanning validation and
independent event labels remain future work. Without visibility/event labels, the
report does not score a hypothesized front or mist as confirmed meteorological truth.
No changes to local correction constants should be justified solely by these first
few scores.
