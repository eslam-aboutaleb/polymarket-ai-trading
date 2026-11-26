"""Deterministic decision and abstention policy. The model has no vote here.

``investigate`` means research interest only. It never implies an approved
order, realized alpha or passed risk checks, and ``execution_eligible`` stays
``False`` on every result.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from app.services.intelligence.market_deep_dive.claim_validation import independent_source_total
from app.services.intelligence.market_deep_dive.schemas import (
    Decision,
    EvidenceItem,
    ForecastDisplay,
    MarketIdentity,
    QuoteAndCost,
    ResearchClaim,
    ResolutionSummary,
)

MIN_INDEPENDENT_SOURCES = 2


@dataclass
class PolicyOutcome:
    decision: Decision
    reason_codes: list[str] = field(default_factory=list)


def enforce_decision_policy(
    *,
    identity: MarketIdentity,
    resolution: ResolutionSummary,
    claims: list[ResearchClaim],
    evidence: list[EvidenceItem],
    forecast: ForecastDisplay | None,
    cost: QuoteAndCost | None,
    upstream_failures: list[str],
    requested_shares_given: bool,
    draft_valid: bool,
    extra_codes: list[str] | None = None,
) -> PolicyOutcome:
    codes: list[str] = list(extra_codes or [])
    abstain = False

    if upstream_failures:
        codes.append("PARTIAL_UPSTREAM_FAILURE")
        abstain = True
    if identity.market_status != "active":
        codes.append("ABSTAIN_MARKET_NOT_ACTIVE")
        abstain = True
    if resolution.verification != "verified":
        codes.append("ABSTAIN_RULES_UNVERIFIED")
        abstain = True

    pivotal = [c for c in claims if c.pivotal]
    unsupported_pivotal = [c for c in pivotal if c.attribution != "attributed"]
    if not draft_valid:
        codes.extend(["INVALID_STRUCTURED_OUTPUT", "ABSTAIN_CLAIMS_UNSUPPORTED"])
        abstain = True
    elif unsupported_pivotal:
        codes.extend(["UNSUPPORTED_PIVOTAL_CLAIM", "ABSTAIN_CLAIMS_UNSUPPORTED"])
        abstain = True
    elif not any(c.attribution == "attributed" for c in pivotal):
        codes.append("ABSTAIN_CLAIMS_UNSUPPORTED")
        abstain = True
    elif independent_source_total(claims, evidence) < MIN_INDEPENDENT_SOURCES:
        codes.append("INSUFFICIENT_INDEPENDENT_EVIDENCE")
        abstain = True

    if forecast is None or forecast.validity_status != "validated":
        codes.append("ABSTAIN_FORECAST_UNVALIDATED")
        abstain = True

    economics_needed = requested_shares_given or (
        forecast is not None and forecast.validity_status == "validated"
    )
    if economics_needed:
        if cost is None or cost.validity_status == "unavailable":
            codes.append("ABSTAIN_QUOTES_UNAVAILABLE")
            abstain = True
        elif cost.validity_status == "stale":
            codes.append("ABSTAIN_QUOTES_STALE")
            abstain = True
        elif cost.validity_status != "executable_modeled":
            reasons = set(cost.validity_reasons)
            if "INSUFFICIENT_BOOK_DEPTH" in reasons:
                codes.append("INSUFFICIENT_BOOK_DEPTH")
            elif "FEES_UNKNOWN" in reasons:
                codes.append("FEES_UNKNOWN")
            else:
                codes.append("ABSTAIN_ECONOMICS_NOT_EXECUTABLE")
            abstain = True
        elif cost.net_ev is None:
            codes.append("ABSTAIN_NET_EV_UNAVAILABLE")
            abstain = True

    if abstain:
        return PolicyOutcome("abstain", list(dict.fromkeys(codes)))
    if cost is None or cost.net_ev is None:  # unreachable: gated above
        return PolicyOutcome("abstain", list(dict.fromkeys([*codes, "ABSTAIN_NET_EV_UNAVAILABLE"])))
    lower = Decimal(cost.net_ev_lower_bound) if cost.net_ev_lower_bound is not None else None
    if Decimal(cost.net_ev) > 0 and lower is not None and lower > 0:
        return PolicyOutcome("investigate", list(dict.fromkeys([*codes, "RESEARCH_INTEREST_ONLY"])))
    return PolicyOutcome("no_clear_edge", list(dict.fromkeys([*codes, "NO_POSITIVE_NET_EDGE"])))
