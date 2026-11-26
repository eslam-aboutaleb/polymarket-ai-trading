"""Server-side canonical market identity, rule summary and quote capture.

The client supplies only a condition ID (and optionally a token ID). Gamma and
CLOB are read here, joined strictly on condition and token, and timestamped.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.intelligence.market_context import MarketContext
from app.services.intelligence.market_deep_dive.schemas import (
    MarketIdentity,
    ResolutionSummary,
)
from app.services.intelligence.resolution_rule_models import ContractSpecification
from app.services.intelligence.resolution_rules import parse_resolution_rule
from app.services.market_data.replay_capture import PublicVenueCapture


class TokenSelectionError(ValueError):
    """The selected token is not one of the market's two mapped outcome tokens."""


@dataclass(frozen=True)
class CanonicalMarket:
    context: MarketContext
    clob: dict[str, Any]
    gamma: dict[str, Any]
    spec: ContractSpecification
    identity: MarketIdentity
    resolution: ResolutionSummary


def _market_status(clob: dict[str, Any], gamma: dict[str, Any]):
    closed = clob.get("closed") is True or gamma.get("closed") is True
    if closed:
        return "closed"
    if (
        clob.get("active") is True
        and clob.get("closed") is False
        and clob.get("accepting_orders") is True
        and gamma.get("active") is True
        and gamma.get("closed") is False
    ):
        return "active"
    if clob.get("active") is False or gamma.get("active") is False:
        return "inactive"
    return "unknown"


def _event_id(gamma: dict[str, Any]) -> str | None:
    events = gamma.get("events")
    if isinstance(events, list) and len(events) == 1 and isinstance(events[0], dict):
        value = events[0].get("id")
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return str(value)
    return None


def resolution_summary(spec: ContractSpecification) -> ResolutionSummary:
    """Map the #16 contract specification without upgrading its review status."""
    verification = (
        spec.review_status if spec.review_status in ("verified", "ambiguous") else "unverified"
    )
    codes = [*spec.abstain_reasons, *spec.ambiguity_flags]
    if spec.review_status == "invalid":
        codes.append("RULE_SPEC_INVALID")
    return ResolutionSummary(
        raw_rule_text_ref=f"{spec.source_uri}#sha256={spec.source_hash}",
        raw_rule_text=spec.raw_rules,
        oracle=spec.oracle,
        cutoff=spec.cutoff_utc,
        comparator=None if spec.comparator == "unknown" else spec.comparator,
        threshold=spec.threshold,
        result_mapping=dict(spec.token_outcomes),
        source_url=spec.source_uri,
        verification=verification,
        warning_codes=list(dict.fromkeys(codes)),
    )


async def canonical_market_context(
    venue: PublicVenueCapture, condition_id: str, selected_token_id: str | None
) -> CanonicalMarket:
    """Fetch Gamma+CLOB, validate identity and the token join; raise on any mismatch."""
    cid = condition_id.lower()
    clob, gamma, context = await venue.market(cid)
    spec = parse_resolution_rule(cid, clob, context.observed_at)
    token_map = {o.outcome: o.token_id for o in context.outcomes}
    selected_outcome = None
    if selected_token_id is not None:
        matches = [o for o, token in token_map.items() if token == selected_token_id]
        if len(matches) != 1:
            raise TokenSelectionError("Selected token does not belong to this market")
        selected_outcome = matches[0]
    identity = MarketIdentity(
        condition_id=cid,
        event_id=_event_id(gamma),
        question=context.question,
        selected_token_id=selected_token_id,
        selected_outcome=selected_outcome,
        token_map=token_map,
        snapshot_id=context.snapshot_id,
        rules_hash=context.rules.content_hash,
        as_of=context.observed_at.astimezone(UTC),
        market_status=_market_status(clob, gamma),
        source_url=context.source_url,
    )
    return CanonicalMarket(context, clob, gamma, spec, identity, resolution_summary(spec))


def now() -> datetime:
    return datetime.now(UTC)
