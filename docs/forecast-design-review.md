# Expert-guided local forecast synthesis: design review

Reviewed 15 November 2026 against the repository's current forecast path. This is
an improvement design, not a claim that the proposed engine is implemented or
outperforms MET Norway. No running forecast rules were changed by this review.

## Aim

Produce a coherent account of what is happening locally, what is likely to change
over the next six hours, why, and what remains uncertain. Use explicit meteorological
reasoning in the manner of a careful observer, supported by a regional numerical
forecast. Do not reduce the task to predicting a weather icon from pressure/humidity.

This is an expert-guided, observation-aware synthesis approach. It can operate
without first fitting a statistical model to months of labelled local outcomes.
It needs only the rolling history required for each diagnostic to become available.
That removes a training prerequisite, not the requirement to verify performance.
Some ML approaches can use pretrained models or transfer learning without months
of site-specific training, so this is an architectural choice, not a universal
limitation of machine learning. MET's own forecasting infrastructure is external.

## What the code does today

| Component | Implemented behavior | Limitation / implication |
| --- | --- | --- |
| Observation ingestion | Dedicated canonical pressure feed; independent SHT45, wind and rain sources | Freshness/quality contracts vary across sources |
| `derived_features.py` | Time-aware pressure, temperature/humidity, dew-point and wind trends; pressure gap/spike checks; circular wind arithmetic | Diagnostics have different coverage needs; direction shifts need better calm/coverage screening |
| `local_forecast.py:interpret` | Text evidence for pressure change, moisture corroboration, possible frontal passage and mist/low-cloud potential | Returns strings, not structured hypotheses with supporting/conflicting evidence |
| `heuristics.py:compute_now_forecast` | Conservative current conditions: wind and recent gauge rain, otherwise cloud unobserved | It does not call the old `_classify_cloud`; this dormant pressure/humidity/lux classifier must not be mistaken for the active path |
| `main.py:_build_forecast` | Observations for now; coherent MET run by valid time for future hours | Local interpretation is copied to each future slot; there is no consolidated event timeline |
| `local_forecast.py:adjust_temperature` | Current model/observed temperature difference, capped at ±3°C and decaying with two-hour time constant; withheld for missing coverage/possible transitions | Fixed engineering defaults; adjustment assumes a persistent local offset and is not skill-validated |
| `heuristics.py:_project_horizon` | MET hourly precipitation and symbols determine future wet/cloud states | Local evidence does not currently revise rain timing; any positive amount has the same “Rain expected” label |
| Confidence in `main.py` | Mostly fixed 50/35 future scores; current heuristic scores | Not calibrated; missing pressure can reduce a valid independent MET forecast's score |
| `confidence.py` | Separate older confidence function exists | Not called by the active build path; variation across future hours is not ensemble uncertainty |
| `snapshot_forecast.py` / `forecast_processor.py` | Archive issued forecasts and compare observations retrospectively | Expand to preserve the exact baseline, evidence and forecast revisions needed to evaluate proposed changes |

## Proposed reasoning pipeline

```text
Quality-controlled observations + correctly timed MET forecast
                         ↓
             Structured evidence ledger
                         ↓
       Local weather-regime hypotheses and contradictions
                         ↓
   Variable-specific, lead-time-limited reconciliation with MET
                         ↓
        Six-hour narrative + event windows + hourly details
                         ↓
          Immutable issue-time record and verification
```

### 1. Establish what can actually be known

Every observation/diagnostic should carry value, observation interval, age, source,
quality, coverage and reason for exclusion. Align local/model comparisons in time
and units. Use the agreed pressure basis; legacy corrected pressure is not an
unqualified meteorological ground truth. Wind exposure and terrain affect comparison
with model-grid wind. Sensor validity and forecast confidence are separate questions.

Keep valid-time intervals explicit. MET instantaneous temperature/wind at time T
and precipitation for [T,T+1h) have different meanings. Do not divide a six-hour
accumulation into fabricated hourly amounts, interpret mm as rain probability, or
label a retrospective gauge interval as instantaneous rain. Distinguish upstream
forecast update time, fetch time and forecast valid time.

Pressure/temperature changes during a sensor restart or source change are not
weather events. Do not infer stable conditions from a missing trend. A saturation
flag may indicate genuine mist/dew or a wet sensor; it does not prove either.
Humidity quality flags must also constrain derived dew-point evidence.

### 2. Build an evidence ledger instead of counting rules

Represent each diagnostic as a structured record, for example:

```json
{
  "id": "pressure_fall_3h",
  "value_hpa": -2.1,
  "window_hours": 3,
  "quality": "ok",
  "supports": ["deteriorating_regime"],
  "does_not_establish": ["rain_start_time", "rain_probability"],
  "dependency_group": "pressure_tendency"
}
```

One-hour and three-hour pressure falls share evidence; they are not independent
votes. Relative humidity, temperature and calculated dew point are mathematically
related. A weather symbol and precipitation amount from the same MET forecast also
are not independent corroboration. Group dependencies before assigning support.

Include counterevidence: “falling pressure, but no moisture increase and MET remains
dry” should stay an explicit disagreement, not become an overconfident rain state.

### 3. Track plausible regimes over time

Candidate interpretations can include an approaching disturbance, possible frontal
passage, post-front unsettled conditions, stable conditions with local mist risk,
and a terrain-driven wind regime. These are hypotheses with alternatives, not
confirmed fronts or fog inferred from a few thresholds.

Use persistence and hysteresis: require a meaningful duration/coverage before
changing regime, retain conflicting evidence, and define when a hypothesis expires.
A sudden wind shift under calm/variable flow must not confirm a front. Distinguish
warming/cooling associated with day/night from moisture changes or air-mass change.
If lux is available, normalize against solar position and a clear-sky expectation;
fixed lux thresholds cannot describe cloud consistently across seasons or darkness.

For mist risk, dew-point spread, valid humidity, cooling, wind and overnight context
can support a cautious hypothesis. Without visibility/cloud-base observations,
do not announce confirmed fog or an exact onset time.

### 4. Reconcile each variable and lead time with MET

Use MET as the regional evolution baseline. Local measurements describe the site
and can identify representativeness differences or a developing mismatch. Neither
source should automatically win all disagreements.

| Lead time | Intended balance (design, not fixed numerical weights) |
| --- | --- |
| Now to about 1 hour | Local observations dominate the description of the site; MET supplies regional evolution |
| About 1–3 hours | Persistence/transition evidence can inform bounded adjustments and explicit alternatives |
| About 3–6 hours | Regional model evolution usually carries more weight; unsupported local extrapolation decays |

Use separate permissions for temperature, wind, cloud and precipitation. A valid
temperature bias correction is not permission to move a rain band. Different
weather regimes may shorten or disable persistence; these lead bands are defaults
to evaluate, not a law or a claim of measured forecast skill.

For precipitation, preserve the timed MET baseline initially. Model/local disagreement
can widen the stated timing uncertainty or add an alternative, but shifting arrival
requires appropriate movement evidence and retrospective validation. A single station
cannot locate or track incoming rain cells. Radar or nearby upwind observations
would be valuable later, subject to availability and actual location coverage.
MET's regional Nordic nowcast product must not be assumed to cover New Zealand.

Use hourly amounts as amounts. If true precipitation probabilities are available
for the selected product/location, retain their semantics; if absent, do not
manufacture percentages. Preserve intensity/type information when supported rather
than labelling every trace amount identically.

Retain the existing bounded temperature correction as a baseline candidate, then
validate persistence by lead/regime before changing its constants. Terrain-specific
wind corrections require evidence about exposure and direction, not a blanket
multiplier inferred from one observation.

### 5. Produce one view, with traceable detail

Return a short six-hour summary, the most likely sequence of changes, supported
event windows, an important alternative, data limitations, and hourly values.
Separate observed facts, interpretation and prediction. Each adjustment should
identify its variable, baseline, magnitude, supporting evidence and expiry.

An illustrative statement, not an actual forecast:

> Pressure is falling and the air is becoming moister. MET forecasts rain later
> this afternoon. These signals support deterioration, but the local station does
> not independently establish when rain will arrive. Wind may change around the
> modelled transition; timing remains uncertain.

Before narrative generation, construct a structured consensus. An optional LLM
could phrase that result, but must not invent missing values, probabilities or
unsupported events. It is not required for this design and is not being introduced
as a dependency by this review. A deterministic formatter should remain sufficient.

Replace apparent precision with separate quality/freshness, model/local agreement,
and event/timing uncertainty. Until calibrated, use clearly labelled qualitative
support rather than presenting heuristic numbers as forecast probabilities. Missing
local evidence limits local refinement, not necessarily the underlying MET guidance.

### 6. Verify from day one without making training a prerequisite

Archive baseline and adjusted forecasts at the same issue/valid times, plus the
observation snapshot, quality flags, rule version, model update identity and reasons.
Measure skill against MET alone and simple persistence, not just against the last
Weatherdash release. Use forward-in-time evaluation; do not tune and report on the
same events or treat overlapping six-hour forecasts as independent cases.

Start with temperature/wind errors by lead, precipitation interval detection and
amount errors, event-window timing and missing-data behavior. Probability calibration
metrics apply only if the system actually issues probabilities. Stratify by regime
and season as sufficient cases accumulate; report sample counts and uncertainty.

The current archive's immutable issue-time keys are a useful starting point.
Add explicit baseline wind/rain and structured evidence. Corrections are not an
improvement merely because they sound plausible: promote them only when their
benefit survives forward evaluation. Historical data supports verification and
optional later calibration; it is not required to begin rule-based inference.

## Recommended implementation order

1. **Evidence contracts and time semantics.** Unify quality/coverage metadata;
   specify interval boundaries, issue/fetch times and source dependencies. Keep
   all present missing-data and source-isolation safeguards.
2. **Structured regime assessment and consolidated output.** Introduce evidence
   records, counterevidence and persistence, then a deterministic six-hour summary.
   Initially retain MET future precipitation and existing temperature defaults.
3. **Quality/uncertainty reporting.** Separate observation health, model agreement
   and forecast uncertainty; retire inactive/conflicting confidence paths after
   checking callers and tests. Add solar-normalized lux only when that sensor's
   readings and site parameters are available.
4. **Verification-led refinements.** Archive exact baselines and decisions; test
   bounded local temperature/wind refinements against unmodified MET and persistence.
5. **Optional regional evidence / language layer.** Add useful external movement
   evidence when available; consider narrative generation only over validated
   structured output, retaining a deterministic fallback.

Acceptance scenarios should include nocturnal humidity rise without moisture gain,
wet-sensor recovery, wind direction changes in calm conditions, falling pressure
with a dry MET forecast, recent rain followed by a dry model hour, possible frontal
passage, stale/contradictory inputs, missing hourly precipitation and a sensor
restart. The expected answer can be “uncertain”; it need not always be a weather icon.

## Sources and scope

These sources support timing and nowcasting context; the proposed rules/architecture
above are engineering recommendations based on this code review, not WMO-prescribed
thresholds or a claim that an expert forecaster has validated them.

- [MET Locationforecast data model](https://docs.api.met.no/doc/locationforecast/datamodel.html):
  instantaneous and period variables, product-dependent availability.
- [MET ForecastJSON semantics](https://docs.api.met.no/doc/ForecastJSON):
  next-hour precipitation covers the hour beginning at its timestamp.
- [MET Locationforecast FAQ](https://docs.api.met.no/doc/locationforecast/FAQ.html):
  topography/altitude and update cadence; repeated fetches do not imply new runs.
- [WMO nowcasting / very-short-range application area](https://space.oscar.wmo.int/applicationareas/view/2_3_nowcasting_very_short_range_forecasting):
  context for combining observations and numerical prediction over short lead times.

## First implementation milestone

The first increment now provides a deterministic `synthesis` object in the forecast
API and a consolidated-outlook panel on the browser dashboard. It contains a
quality-qualified evidence ledger with dependency groups and assessment/input
timestamps, cautious regime candidates, local/MET pattern consistency, limitations,
and MET wet/dry windows explicitly bounded to the next six hours. Missing hours
are not joined across, and no rainfall amounts are prorated or probabilities invented.

Pressure acceleration is grouped with pressure tendency rather than counted as
independent support. Flagged/stale moisture cannot corroborate deterioration or mist.
Direction shifts require non-calm wind and adequately populated history windows.
Current regime candidates are not yet persistence-tracked; the API explicitly says
so. Contradictory pressure/dry-MET patterns stay visible without overriding MET.

Existing forecast values and temperature-adjustment defaults remain the baseline.
The new schema also retains explicit baseline wind/rain and archives synthesis,
observations and derived features at issue time. Forecast generation is scheduled
every five minutes to better match short-lived sensor evidence; expired synthesis
is withheld independently of the regional forecast cache. Older caches remain readable.

Next: add regime persistence/hysteresis and expanded baseline verification before
introducing new local corrections or changes to precipitation timing. Confidence
numbers remain the legacy heuristic scores, separate from the new qualitative
support and limitations; calibrated probabilities are still not implemented.

## Second implementation milestone — 16 November 2026

Regime persistence/hysteresis and expanded numerical verification are now
implemented. This supersedes the first milestone's statement that regimes are
not yet persistence-tracked. The previous successful cache carries state; fresh
cycles, entry duration, wider exit bands, quality withholding and bounded retention
are explicit. No forecast values or MET rain timing are changed by that tracking.

New archives capture issue-time persistence and MET baselines. Scheduled
verification matches quality-qualified observations, requires complete gauge hours,
reports paired metrics by version/lead/regime and exposes `/api/verification` plus
a browser comparison table. Independent front/fog truth, calibrated probabilities
and statistical significance of any improvement are not claimed. See
[the policy and operations guide](regimes-and-verification.md).

## Runtime interpretation — 16 November 2026

The runtime LLM is now implemented as the primary interpreter, superseding the
historical proposal that treated it as optional wording over a deterministic
consensus. Deterministic preparation, persistence and checks remain valuable, but
factual problems and advisory disagreements go back to the model for correction or
justification. A failed loop is visible; no deterministic forecast is silently
substituted. See [the runtime contract](runtime-llm.md).
