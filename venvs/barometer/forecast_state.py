from enum import Enum
from pydantic import BaseModel, Field


class PressureTendency(str, Enum):
    UNKNOWN = "unknown"
    RAPID_FALL = "rapid_fall"
    SLOW_FALL = "slow_fall"
    STABLE = "stable"
    RISING = "rising"


class AtmosphericState(str, Enum):
    UNKNOWN = "unknown"
    SUNNY = "sunny"
    MOSTLY_SUNNY = "mostly_sunny"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    MOSTLY_CLOUDY = "mostly_cloudy"
    OVERCAST = "overcast"
    DRIZZLE = "drizzle"
    SHOWERS = "showers"
    RAIN_LIKELY = "rain_likely"
    HEAVY_RAIN = "heavy_rain"
    WINDY = "windy"
    CLEARING = "clearing"
    FOGGY = "foggy"
    STABLE = "stable"


STATE_LABELS: dict[AtmosphericState, str] = {
    AtmosphericState.UNKNOWN: "Forecast unavailable",
    AtmosphericState.STABLE: "Settled fine",
    AtmosphericState.SUNNY: "Sunny",
    AtmosphericState.MOSTLY_SUNNY: "Mostly sunny",
    AtmosphericState.PARTLY_CLOUDY: "Partly cloudy",
    AtmosphericState.CLOUDY: "Cloudy",
    AtmosphericState.MOSTLY_CLOUDY: "Mostly cloudy",
    AtmosphericState.OVERCAST: "Overcast",
    AtmosphericState.DRIZZLE: "Light drizzle",
    AtmosphericState.SHOWERS: "Showers",
    AtmosphericState.RAIN_LIKELY: "Rain",
    AtmosphericState.HEAVY_RAIN: "Heavy rain",
    AtmosphericState.WINDY: "Windy",
    AtmosphericState.CLEARING: "Clearing up",
    AtmosphericState.FOGGY: "Foggy",
}

STATE_ICONS: dict[AtmosphericState, str] = {
    AtmosphericState.UNKNOWN: "?",
    AtmosphericState.STABLE: "\u2600\ufe0f",
    AtmosphericState.SUNNY: "\u2600\ufe0f",
    AtmosphericState.MOSTLY_SUNNY: "\U0001f324\ufe0f",
    AtmosphericState.PARTLY_CLOUDY: "\u26c5",
    AtmosphericState.CLOUDY: "\u2601\ufe0f",
    AtmosphericState.MOSTLY_CLOUDY: "\U0001f325\ufe0f",
    AtmosphericState.OVERCAST: "\u2601\ufe0f",
    AtmosphericState.DRIZZLE: "\u2601\ufe0f\u2614",
    AtmosphericState.SHOWERS: "\U0001f326\ufe0f",
    AtmosphericState.RAIN_LIKELY: "\U0001f327\ufe0f",
    AtmosphericState.HEAVY_RAIN: "\U0001f328\ufe0f",
    AtmosphericState.WINDY: "\U0001f4a8",
    AtmosphericState.CLEARING: "\u26c5",
    AtmosphericState.FOGGY: "\U0001f32b\ufe0f",
}


class RooftopSnapshot(BaseModel):
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    wind_speed_kmh: float | None = None
    wind_gust_kmh: float | None = None
    wind_direction_deg: float | None = None
    rain_detected: bool = False
    lux: float | None = None
    lux_condition: str | None = None
    dewpoint_c: float | None = None
    observation_times: dict[str, str] = Field(default_factory=dict)
    humidity_quality: str = "unknown"
    humidity_quality_reason: str | None = None
    rain_quality: str = "unknown"
    rain_recent_mm: float | None = None


class DerivedFeatures(BaseModel):
    pressure_delta_1h: float | None = None
    pressure_delta_3h: float | None = None
    humidity_delta_1h: float | None = None
    temp_delta_1h: float | None = None
    dewpoint_delta_1h: float | None = None
    wind_speed_delta_1h: float | None = None
    wind_dir_change_1h: float | None = None
    rain_persistence_1h: float | None = None


class CorrectedForecast(BaseModel):
    forecast_temp: float | None = None
    forecast_precip_mm: float | None = None
    forecast_cloud_pct: float | None = None
    forecast_wind_speed: float | None = None
    forecast_wind_dir: float | None = None
    forecast_pressure: float | None = None
    forecast_humidity: float | None = None


class Scores(BaseModel):
    rain_score: float = 0.0
    clearing_score: float = 0.0
    wind_score: float = 0.0
    stability_score: float = 0.0


class ForecastSlot(BaseModel):
    hour_offset: int
    time_label: str = ""
    is_night: bool | None = None
    state: AtmosphericState
    state_label: str
    icon: str
    temperature_c: float | None = None
    wind_dir_deg: float | None = None
    wind_speed_kmh: float | None = None
    confidence: int = 50
    pressure_tendency: PressureTendency = PressureTendency.STABLE
    scores: Scores | None = None
    source: str = "local-instruments"
    degraded: bool = False
    precipitation_mm: float | None = None
    evidence: list[str] = Field(default_factory=list)
    forecast_pressure_hpa: float | None = None
    forecast_humidity_pct: float | None = None
    baseline_temperature_c: float | None = None
    baseline_wind_speed_kmh: float | None = None
    baseline_wind_dir_deg: float | None = None
    baseline_precipitation_mm: float | None = None
    temperature_adjustment_c: float = 0.0
    confidence_kind: str = "heuristic_evidence_score"
    forecast_valid_time: str | None = None
    forecast_issued: float | None = None


class EvidenceItem(BaseModel):
    id: str
    dependency_group: str
    message: str
    value: float | None = None
    unit: str | None = None
    window_hours: float | None = None
    quality: str = "ok"
    source: str = "local-derived"
    observed_at: str | None = None
    assessed_at: str
    observation_times: dict[str, str] = Field(default_factory=dict)
    supports: list[str] = Field(default_factory=list)


class RegimeAssessment(BaseModel):
    id: str
    support: str
    evidence_ids: list[str] = Field(default_factory=list)
    dependency_groups: list[str] = Field(default_factory=list)
    caveat: str | None = None
    persistence: str = "candidate"
    first_seen: str | None = None
    persistent_since: str | None = None
    fresh_cycles: int = 1


class RegimeMemory(BaseModel):
    assessment: RegimeAssessment
    first_seen: str
    last_seen: str
    last_qualifying: str
    observation_times: dict[str, str] = Field(default_factory=dict)
    fresh_cycles: int = 1
    persistent_since: str | None = None
    exit_started: str | None = None
    exit_cycles: int = 0


class RegimeTransition(BaseModel):
    id: str
    state: str
    reason: str


class ForecastWindow(BaseModel):
    start: str
    end: str
    state: str
    source: str = "metno"
    timing_basis: str = "Intersecting MET hourly intervals; not precise onset/cessation"


class ForecastSynthesis(BaseModel):
    as_of: str
    valid_until: str
    summary: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    regimes: list[RegimeAssessment] = Field(default_factory=list)
    windows: list[ForecastWindow] = Field(default_factory=list)
    model_local_agreement: str = "not_assessed"
    conflicts: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    uncertainty: str
    tracking_version: str = "regime_v1"
    regime_memory: dict[str, RegimeMemory] = Field(default_factory=dict)
    regime_transitions: list[RegimeTransition] = Field(default_factory=list)


class ForecastStrip(BaseModel):
    synthesis: ForecastSynthesis | None = None
    generated_at: str
    generated_at_iso: str | None = None
    model_version: str = "rules_v3_persistence"
    evidence: list[str] = Field(default_factory=list)
    degraded: bool = False
    slots: list[ForecastSlot] = Field(default_factory=list)
    rooftop: RooftopSnapshot | None = None
    derived: DerivedFeatures | None = None
    pressure_tendency: PressureTendency = PressureTendency.STABLE
    pressure_history: list[float | None] = Field(default_factory=list)
    pressure_history_times: list[str] = Field(default_factory=list)
    pressure_history_timestamps: list[str] = Field(default_factory=list)
