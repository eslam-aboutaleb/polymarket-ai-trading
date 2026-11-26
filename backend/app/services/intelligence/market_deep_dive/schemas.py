"""Versioned public contract for ``structured_v2`` Market Deep Dive results.

``None`` always means *unknown*. Unknown prices are never filled with 0.5,
unknown fees with zero, and a missing forecast is never replaced by a neutral
percentage. Decimals serialize as strings; datetimes must be timezone-aware.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

SCHEMA_VERSION = "market-deep-dive-v2"

DecimalStr = Annotated[Decimal, PlainSerializer(lambda v: str(v), return_type=str)]
Probability = Annotated[
    Decimal,
    Field(ge=Decimal(0), le=Decimal(1), allow_inf_nan=False),
    PlainSerializer(lambda v: str(v), return_type=str),
]

Verification = Literal["verified", "unverified", "ambiguous"]
Direction = Literal["supports", "contradicts", "context"]
Decision = Literal["investigate", "no_clear_edge", "abstain"]
EvidenceVerification = Literal["excerpt_matched", "venue_text", "unverified"]
ForecastValidity = Literal["validated", "unvalidated", "unavailable"]
CostValidity = Literal["executable_modeled", "indicative", "stale", "unavailable"]

MAX_REQUESTED_SHARES = Decimal(1_000_000_000)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketIdentity(_Model):
    condition_id: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    event_id: str | None = None
    question: str
    selected_token_id: str | None = None
    selected_outcome: Literal["YES", "NO"] | None = None
    token_map: dict[Literal["YES", "NO"], str]
    snapshot_id: str | None = None
    rules_hash: str
    as_of: AwareDatetime
    market_status: Literal["active", "closed", "inactive", "unknown"]
    source_url: str


class ResolutionSummary(_Model):
    raw_rule_text_ref: str
    raw_rule_text: str = ""
    oracle: str | None = None
    observation_window_utc: tuple[AwareDatetime, AwareDatetime] | None = None
    cutoff: AwareDatetime | None = None
    comparator: str | None = None
    threshold: DecimalStr | None = None
    result_mapping: dict[str, str] = Field(default_factory=dict)
    source_url: str
    verification: Verification
    warning_codes: list[str] = Field(default_factory=list)


class EvidenceItem(_Model):
    evidence_id: str
    url: str
    canonical_url: str
    publisher: str
    published_at: AwareDatetime | None = None
    retrieved_at: AwareDatetime
    available_at: AwareDatetime | None = None
    source_family: Literal["venue_rules", "search_excerpt"]
    content_hash: str
    excerpt: str | None = None
    verification: EvidenceVerification
    limitations: list[str] = Field(default_factory=list)


class ResearchClaim(_Model):
    claim_id: str
    statement: str
    direction: Direction
    pivotal: bool = False
    cited_evidence_ids: list[str] = Field(default_factory=list)
    independent_source_count: int = 0
    attribution: Literal["attributed", "unsupported"]
    uncertainty: str | None = None
    conflicts: list[str] = Field(default_factory=list)


class ForecastDisplay(_Model):
    raw_probability: Probability | None = None
    calibrated_probability: Probability | None = None
    interval: tuple[Probability, Probability] | None = None
    model_id: str | None = None
    model_version: str | None = None
    forecast_id: int | None = None
    forecast_recorded_at: AwareDatetime | None = None
    calibration_cohort: str | None = None
    calibration_report_id: str | None = None
    resolved_sample_count: int = 0
    brier: DecimalStr | None = None
    log_loss: DecimalStr | None = None
    market_baseline_brier: DecimalStr | None = None
    market_baseline_log_loss: DecimalStr | None = None
    market_benchmark_p: Probability | None = None
    market_benchmark_observed_at: AwareDatetime | None = None
    validity_status: ForecastValidity
    validity_reasons: list[str] = Field(default_factory=list)
    label: Literal["validated_model_forecast", "experimental_unvalidated", "none"] = "none"


class QuoteAndCost(_Model):
    venue: Literal["polymarket-clob"] = "polymarket-clob"
    token_id: str | None = None
    side: Literal["BUY"] = "BUY"
    snapshot_ref: str | None = None
    bid: DecimalStr | None = None
    ask: DecimalStr | None = None
    indicative_prices: dict[str, DecimalStr | None] = Field(default_factory=dict)
    quote_source_at: AwareDatetime | None = None
    quote_received_at: AwareDatetime | None = None
    quote_age_ms: int | None = None
    requested_size: DecimalStr | None = None
    depth_coverage: DecimalStr | None = None
    vwap: DecimalStr | None = None
    fee_schedule_id: str | None = None
    costs: dict[str, DecimalStr] | None = None
    net_ev: DecimalStr | None = None
    net_ev_lower_bound: DecimalStr | None = None
    break_even_probability: DecimalStr | None = None
    validity_status: CostValidity
    validity_reasons: list[str] = Field(default_factory=list)


class Completeness(_Model):
    sections: dict[str, Literal["complete", "partial", "missing", "failed"]]
    upstream_failures: list[str] = Field(default_factory=list)
    persisted: bool = False


class MarketDeepDiveResult(_Model):
    schema_version: Literal["market-deep-dive-v2"] = SCHEMA_VERSION
    analysis_id: str
    identity: MarketIdentity
    resolution: ResolutionSummary
    summary: str = ""
    key_insights: list[str] = Field(default_factory=list, max_length=5)
    evidence: list[EvidenceItem]
    claims: list[ResearchClaim]
    forecast: ForecastDisplay | None = None
    price_and_cost: QuoteAndCost | None = None
    risks: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    decision: Decision
    reason_codes: list[str]
    completeness: Completeness
    versions: dict[str, str | None]
    created_at: AwareDatetime
    execution_eligible: Literal[False] = False


class DeepDiveRequest(_Model):
    """Authoritative identity only; titles, prices, dates and volume are never accepted."""

    condition_id: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    selected_token_id: str | None = Field(default=None, pattern=r"^[0-9]{1,120}$")
    requested_shares: Decimal | None = Field(default=None, allow_inf_nan=False)
    include_research: bool = True

    @field_validator("requested_shares")
    @classmethod
    def _bounded(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        if not value.is_finite() or value <= 0 or value > MAX_REQUESTED_SHARES:
            raise ValueError("requested_shares out of bounds")
        if value.as_tuple().exponent < -8:  # type: ignore[operator]
            raise ValueError("requested_shares has too many decimal places")
        return value


class DeepDiveDraftClaim(_Model):
    """Model-authored claim. The model cannot set status, evidence IDs or verification."""

    model_config = ConfigDict(extra="forbid", strict=True)

    statement: str = Field(min_length=1, max_length=1000)
    direction: Direction
    source_url: str = Field(max_length=2000)
    quote: str = Field(max_length=3000)
    pivotal: bool
    uncertainty: str | None = Field(default=None, max_length=500)


class DeepDiveDraft(_Model):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=2000)
    key_insights: list[str] = Field(default_factory=list, max_length=5)
    claims: list[DeepDiveDraftClaim] = Field(max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=8)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=8)
    missing_information: list[str] = Field(default_factory=list, max_length=12)
