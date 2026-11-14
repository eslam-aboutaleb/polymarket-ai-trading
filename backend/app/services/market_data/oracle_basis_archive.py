"""Read-side identity/clock checks over server-owned immutable feed receipts.

No public input can insert a receipt or assert oracle authenticity. Public
Polymarket displayed references and RTDS spot are deliberately absent from the
Chainlink authoritative origin mapping. Missing licensed/reference history
remains source-unknown and cannot become a definitive settlement probability.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.models.market_data.venue_observation import VenueObservation
from app.services.intelligence.resolution_rules import get_rule_snapshot_as_of
from app.services.market_data.oracle_basis import evaluate_oracle_basis
from app.services.market_data.venue_observations import serialize

ORIGINS = {
    "chainlink": "chainlink-data-streams",
    "binance": "binance-public-rest",
    "coinbase": "coinbase-public-rest",
    "kraken": "kraken-public-rest",
}


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def evaluate_archived_oracle_basis(db, request):
    now = datetime.now(UTC)
    if request.as_of is None:
        request = request.model_copy(update={"as_of": now})
    spec = request.specification
    reasons = []
    verified = False
    try:
        if request.as_of.tzinfo is None or request.as_of > now:
            raise ValueError("ABSTAIN_INVALID_AS_OF")
        rule = get_rule_snapshot_as_of(db, spec.condition_id, request.as_of or spec.cutoff_utc)
        if (
            rule is None
            or rule.review_status != "verified"
            or rule.source_hash != spec.rule_hash
            or rule.oracle != spec.authoritative_provider
            or rule.oracle_symbol != spec.eligible_instrument
            or rule.cutoff_utc != spec.cutoff_utc
            or rule.threshold != request.strike
            or rule.comparator != request.comparator
            or spec.aggregation != "last"
            or spec.rounding_mode != "none"
            or spec.fallback_provider is not None
        ):
            raise ValueError("ABSTAIN_UNVERIFIED_RECORDED_ORACLE_SPECIFICATION")
        for point in [*request.spot_observations, *request.settlement_observations]:
            row = db.get(VenueObservation, point.capture_id) if point.capture_id else None
            origin = ORIGINS.get(point.source.strip().casefold())
            if (
                row is None
                or origin is None
                or row.source != origin
                or row.kind not in {"oracle_reference", "external_reference"}
                or (row.condition_id is not None and row.condition_id != spec.condition_id)
            ):
                raise ValueError("ABSTAIN_UNVERIFIED_FEED_RECEIPT")
            raw = serialize(row)["raw"]
            if (
                raw.get("provider") != point.source
                or raw.get("instrument") != point.instrument
                or Decimal(str(raw.get("value"))) != point.value
                or row.event_at is None
                or aware(row.event_at) != point.source_at
                or aware(row.received_at) != point.received_at
            ):
                raise ValueError("ABSTAIN_FEED_RECEIPT_IDENTITY_OR_CLOCK_MISMATCH")
        verified = bool(request.spot_observations and request.settlement_observations)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        reason = str(exc)
        reasons.append(
            reason
            if reason.startswith("ABSTAIN_")
            else "ABSTAIN_UNVERIFIED_RECORDED_ORACLE_LINEAGE"
        )
    result = evaluate_oracle_basis(request, lineage_verified=verified)
    result.abstain_reasons = list(dict.fromkeys(reasons + result.abstain_reasons))
    return result
