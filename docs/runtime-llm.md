# Runtime LLM interpretation and correction

Implemented 16 November 2026. The LLM is the primary six-hour interpreter, not a
formatter for a deterministic forecast. The deployed provider is DeepSeek directly.
The previously configured `deepseek-chat` name was no longer advertised by the
provider; the verified available `deepseek-flash` model is used instead.

## Runtime loop

1. Collect current observations, pressure history (six hours), temperature/humidity/
   wind/gust history (four hours), rain intervals (six hours), quality flags, derived
   trends and coherent MET forecasts. Preserve missing values, timestamps and units.
2. Supply those facts, advisory diagnostics, persisted regime assessments, an exact
   evidence-reference catalog and the required JSON schema to the LLM.
3. Receive its six hourly forecasts, summary, compact basis, uncertainty, alternative,
   input claims and references. Current numerical readings remain instrument data.
4. Validate facts, timing, schema, ranges, references, acknowledged limitations and
   kiosk text fit. Return specific problems to the LLM with its previous response.
5. Let the LLM correct hard problems. For advisory departures, let it revise or
   justify using supplied evidence. A justified disagreement is published visibly;
   it is not a veto by the rule-based forecast. At most two revisions follow the
   initial request, within an 80-second total budget and 25-second request timeout.
6. Publish only a validated interpretation. Otherwise publish an explicit
   `unavailable` or `disputed` status, retain current measurements and MET reference
   fields, and withhold future interpreted values. No rule-based forecast is
   silently substituted. A later scheduled update tries again.

Display requests read the cache and never call the provider. The worker runs every
five minutes. The cache records actual publication time separately from input time;
responses arriving after their first forecast target are rejected as outdated.
The current observed pressure still expires independently. A published LLM outlook
is retained for at most ten minutes before a fresh interpretation is needed.

## Deterministic checks: role and limits

Hard checks cover known machine-checkable constraints: exact input identity and
valid times and summary clock times within the six-hour window, impossible/nonfinite
values, fabricated structured observation claims,
unknown/null supporting references, forgotten data limitations, unsupported
probability percentages, instantaneous-rain claims from an interval gauge, and
kiosk-size violations. Schema text limits are 240/220/120 characters for outlook,
basis and uncertainty, with additional wrapping checks. The model is asked to aim
shorter. Long text is sent back for revision, not silently shortened.

Temperature departures over 3°C, wind-speed departures over 15 km/h, direction
changes of at least 45° from MET, meaningful rain departures/missing MET rain filled
by prediction, and high support despite limitations trigger advisory challenges.
These thresholds are engineering screening defaults, not trained meteorological
truths. A model can keep a departure after supplying a substantive justification.
Hard constraints cannot be waived by an explanation.

The checker cannot prove every sentence meteorologically correct. Structured claims
and references improve traceability but do not guarantee that all prose is sound.
Forecast verification and review of the exchange remain necessary; neither an LLM
nor the deterministic diagnostics establish confirmed fronts/fog without independent
observations. No calibrated probabilities are generated.

## Data semantics

Wind observations/history and changes are normalized to km/h before prompting.
Pressure is already firmware-corrected hPa and must not be corrected again. Rain
readings include the completed gauge interval; missing rain is null, not dry.
MET rain amounts describe the hour beginning at the supplied valid time. The
current slot is observational; the following six slots use explicit next-clock-hour
targets. Summary windows are bounded to the next six hours; amounts are not prorated.

MET rows are keyed by hour offset `"1"` through `"6"`, avoiding array-index ambiguity.
References such as `met_forecast.2.temperature_c` identify the exact supplied value.
History aggregates may have partial coverage; gaps are not filled. Flags and
limitations constrain interpretation. Raw lux is supplied when available, but
`lux_expected_current` remains null until solar/site normalization is configured;
the model is told not to infer cloud from unnormalized light alone.

## Configuration

The scheduled generator needs a private environment file, for example
`/etc/weatherdash-llm.env`, root-owned mode 0600:

```ini
LLM_ENABLED=true
LLM_ENDPOINT=https://api.deepseek.com/chat/completions
LLM_MODEL=deepseek-flash
LLM_API_KEY=replace-with-provider-key
LLM_AUDIT_DB=/var/lib/weatherdash/llm_audit.sqlite3
```

Load it with a systemd service drop-in for `local-forecast.service`:

```ini
[Service]
EnvironmentFile=/etc/weatherdash-llm.env
```

The worker needs write permission to the audit database's parent directory. The
API service does not need the key. Do not commit the private file or copy its
values into prompts, logs or archived payloads. DeepSeek JSON mode is used;
non-thinking mode is requested for bounded operational latency, while concise
public rationale is required. No hidden chain-of-thought is requested or stored.
The existing key is reused; it is never displayed by deployment tooling.

`LLM_ENABLED=false` runs the deterministic reference engine explicitly, useful for
offline development. It is not a fallback triggered by an LLM failure. Offline
tests disable inference and mock the transport. The old legacy refinement caller
is disabled rather than sharing the new runtime configuration accidentally.

## Provenance and verification

The forecast exposes interpreter status, model, input ID, input time, publication
time, attempts and historical validation findings. Each published LLM hour is
marked `source=llm-runtime` and uses qualitative support, not a confidence percentage.
The strip displays that source/status and its concise basis. Diagnostic regimes
are explicitly labelled sanity-check diagnostics, not the model's conclusions.

SQLite audit records contain the exact weather payload and each returned response,
validation findings, provider-reported model and token usage. They contain neither
provider keys/headers nor private reasoning content. `forecast_synthesis` records
retain status/input/model/attempt metadata and findings. Hourly forecasts retain MET
and issue-time persistence baselines for paired verification, with a separate model
version for runtime interpretation. Future pressure/humidity are retained as MET
reference fields, not misrepresented or scored as LLM predictions when the response
schema does not predict them.

The correction protocol has been tested with mocks and with real DeepSeek calls:
a deliberately corrupted **preview-only** claim was rejected, returned to the
provider and corrected on the next attempt. Preview data was not published to the
live cache or sensor topics.

## Operational acceptance

Check the scheduled job succeeds, `/api/forecast` reports `interpreter_status` of
`validated`, future slots are `llm-runtime`, and the SVG shows the complete basis
without scrolling or ellipses. Confirm an audited correction is traceable and that
failed validation does not leave deterministic future values disguised as LLM output.
Inspect `/api/verification` only after issued targets mature; immediate skill or
probability-calibration claims are not supported.

Official interface references: [DeepSeek JSON output](https://api-docs.deepseek.com/guides/json_mode/),
[chat completions](https://api-docs.deepseek.com/api/create-chat-completion/), and
[thinking mode controls](https://api-docs.deepseek.com/guides/thinking_mode/).
