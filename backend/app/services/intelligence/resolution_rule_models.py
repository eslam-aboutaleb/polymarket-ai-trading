"""Public schemas for immutable resolution rules and evidence-backed payoff observations."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SKILL_VERSION = "resolution-rule-analysis-v1"
SCHEMA_VERSION = "resolution-rule-v1"

Comparator = Literal[">", ">=", "<", "<=", "==", "unknown"]
ReviewStatus = Literal["verified", "unverified", "ambiguous", "invalid"]
PayoffOutcome = Literal["YES", "NO", "VOID", "ABSTAIN"]


class EvidenceSpan(BaseModel):
    """Exact character-offset citation into the source text or question."""

    model_config = ConfigDict(extra="forbid")

    field: str
    quote: str
    start: int
    end: int
    source_url: str


class ContractSpecification(BaseModel):
    """Versioned, evidence-backed specification of a binary prediction market's rules."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["resolution-rule-v1"] = SCHEMA_VERSION
    skill_version: str = SKILL_VERSION
    condition_id: str
    question: str
    token_outcomes: dict[str, str]
    subject: str | None = None
    metric: str | None = None
    comparator: Comparator = "unknown"
    threshold: Decimal | None = None
    threshold_precision: int | None = None
    units: str | None = None
    cutoff_utc: datetime | None = None
    local_timezone: str | None = None
    cutoff_inclusive: bool | None = None
    oracle: str | None = None
    oracle_symbol: str | None = None
    fallback_hierarchy: list[str] = Field(default_factory=list)
    rounding_policy: str | None = None
    invalid_void_policy: str | None = None
    dispute_policy: str | None = None
    source_uri: str
    source_hash: str
    raw_rules: str = ""
    raw_market: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime
    valid_from: datetime
    review_status: ReviewStatus
    ambiguity_flags: list[str] = Field(default_factory=list)
    abstain_reasons: list[str] = Field(default_factory=list)
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    execution_eligible: Literal[False] = False


# Alias matching issue specification terminology.
ResolutionRuleVersion = ContractSpecification


class PayoffObservation(BaseModel):
    """Observed oracle data point supplied to ``resolve_payoff``."""

    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    oracle: str
    source_at: datetime | None = None
    oracle_symbol: str | None = None
    value: Decimal | None = None
    uma_status: str | None = None
    early_finalized: bool = False


class PayoffResolution(BaseModel):
    """Deterministic payoff decision over declared states."""

    model_config = ConfigDict(extra="forbid")

    condition_id: str
    source_hash: str
    outcome: PayoffOutcome
    payout_yes: Decimal | None = None
    payout_no: Decimal | None = None
    reason: str


class RuleChangeReport(BaseModel):
    """Comparison between two versions of a market's resolution rule."""

    model_config = ConfigDict(extra="forbid")

    condition_id: str
    previous_hash: str
    current_hash: str
    changed: bool
    invalidates_forecasts: bool
    reasons: list[str] = Field(default_factory=list)


class ResolutionRuleExplanation(BaseModel):
    """Read-only explanation returned to API callers and research agents."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["resolution-rule-v1"] = SCHEMA_VERSION
    specification: ContractSpecification
    tradable_edge_allowed: bool
    change_report: RuleChangeReport | None = None
    execution_eligible: Literal[False] = False
