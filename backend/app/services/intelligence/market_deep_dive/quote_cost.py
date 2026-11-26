"""Read-only quote and executable-cost adapter over the native book model (#18/#15).

Books are captured in memory from the public CLOB and passed through the
existing ``book_research.analyze_values`` walk, so no second cost engine and no
database write exist here. A midpoint or the bid is never presented as an
executable price: ``vwap`` comes only from walking displayed ask depth.
"""

from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.intelligence.market_deep_dive.context_builder import CanonicalMarket
from app.services.intelligence.market_deep_dive.schemas import ForecastDisplay, QuoteAndCost
from app.services.market_data.book_research import ResearchQuery, analyze_values
from app.services.market_data.replay_capture import PublicVenueCapture, digest
from app.services.market_data.valuation_fees import archived_taker_fee

QUOTE_MAX_AGE_SECONDS = 60
_Q18 = Decimal(1).scaleb(-18)


async def capture_book_values(
    venue: PublicVenueCapture, market: CanonicalMarket
) -> tuple[list[dict[str, Any]], list[str]]:
    """Capture the metadata plus both token books as in-memory observation values."""
    context = market.context
    meta_raw = {
        "event_type": "market_metadata",
        "market": context.condition_id,
        "asset_id": context.outcomes[0].token_id,
        "clob": market.clob,
        "gamma": market.gamma,
        "context": context.model_dump(mode="json"),
    }
    values = [
        {
            "snapshot_id": digest(["deep-dive-metadata", context.snapshot_id]),
            "token_id": context.outcomes[0].token_id,
            "condition_id": context.condition_id,
            "connection_id": "deep-dive-rest",
            "event_at": None,
            "received_at": context.observed_at.isoformat(),
            "raw": meta_raw,
        }
    ]
    failures: list[str] = []
    for outcome in context.outcomes:
        try:
            raw_book, book = await venue.book(outcome.token_id, context.condition_id)
        except Exception:  # noqa: BLE001 - any capture failure is an unavailable quote
            failures.append(outcome.token_id)
            continue
        values.append(
            {
                "snapshot_id": book.snapshot_id,
                "token_id": outcome.token_id,
                "condition_id": context.condition_id,
                "connection_id": "deep-dive-rest",
                "event_at": book.source_at.isoformat() if book.source_at else None,
                "received_at": book.observed_at.isoformat(),
                "raw": {**raw_book, "event_type": "book", "_rule_hash": context.rules.content_hash},
            }
        )
    return values, failures


def _indicative(market: CanonicalMarket) -> dict[str, Decimal | None]:
    return {
        o.outcome: (None if o.indicative_price is None else Decimal(str(o.indicative_price)))
        for o in market.context.outcomes
    }


def build_quote_and_cost(
    market: CanonicalMarket,
    values: list[dict[str, Any]],
    capture_failures: list[str],
    requested_shares: Decimal | None,
    *,
    as_of: datetime | None = None,
) -> QuoteAndCost:
    as_of = as_of or datetime.now(UTC)
    outcome = market.identity.selected_outcome
    token = market.identity.selected_token_id
    indicative = _indicative(market)
    base: dict[str, Any] = {
        "token_id": token,
        "indicative_prices": indicative,
        "requested_size": requested_shares,
    }
    reasons: list[str] = []
    if outcome is None:
        return QuoteAndCost(
            **base, validity_status="indicative", validity_reasons=["NO_TOKEN_SELECTED"]
        )
    if token in capture_failures or not any(v["token_id"] == token for v in values[1:]):
        return QuoteAndCost(
            **base, validity_status="unavailable", validity_reasons=["BOOK_CAPTURE_UNAVAILABLE"]
        )
    shares = requested_shares or Decimal(1)
    query = ResearchQuery(
        condition_id=market.identity.condition_id,
        as_of=as_of,
        outcome=outcome,
        side="BUY",
        sizes=[shares],
        fee_bps=Decimal(0),
        fee_version="venue-zero-fee-v1",
        max_age_seconds=QUOTE_MAX_AGE_SECONDS,
    )
    try:
        analysis = analyze_values(query, values, as_of)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return QuoteAndCost(
            **base, validity_status="unavailable", validity_reasons=["BOOK_MODEL_UNAVAILABLE"]
        )
    book = (analysis.get("books") or {}).get(outcome)
    if not book:
        return QuoteAndCost(
            **base,
            validity_status="unavailable",
            validity_reasons=[analysis.get("reason") or "BOOK_UNAVAILABLE"],
        )
    book_value = next(v for v in values[1:] if v["token_id"] == token)
    source_at = book_value["event_at"]
    received_at = book_value["received_at"]
    source_dt = datetime.fromisoformat(source_at) if source_at else None
    age_ms = None
    if source_dt is not None:
        age_ms = max(0, int((as_of - source_dt).total_seconds() * 1000))
    fields: dict[str, Any] = {
        **base,
        "snapshot_ref": book.get("book_hash") or book_value["snapshot_id"],
        "quote_source_at": source_dt,
        "quote_received_at": datetime.fromisoformat(received_at),
        "quote_age_ms": age_ms,
    }
    if book["bids"]:
        fields["bid"] = Decimal(str(book["bids"][0]["price"]))
    if book["asks"]:
        fields["ask"] = Decimal(str(book["asks"][0]["price"]))
    stale = (
        book["grade"] != "VERIFIED_BOOK" or age_ms is None or age_ms > QUOTE_MAX_AGE_SECONDS * 1000
    )
    if stale:
        reasons.append("QUOTE_STALE_OR_UNVERIFIED")
        reasons.extend(book.get("reasons") or [])
        return QuoteAndCost(**fields, validity_status="stale", validity_reasons=reasons)
    if requested_shares is None:
        return QuoteAndCost(
            **fields, validity_status="indicative", validity_reasons=["NO_REQUESTED_SIZE"]
        )
    estimate = (analysis.get("estimates") or [None])[0]
    if not estimate or estimate["status"] != "modeled":
        return QuoteAndCost(
            **fields,
            validity_status="indicative",
            validity_reasons=[
                "INSUFFICIENT_BOOK_DEPTH",
                (estimate or {}).get("reason") or "NO_ESTIMATE",
            ],
        )
    coverage = Decimal(estimate["coverage"])
    fields["depth_coverage"] = coverage
    fields["vwap"] = Decimal(estimate["vwap"])
    if estimate.get("partial") or Decimal(estimate["matched_shares"]) != requested_shares:
        return QuoteAndCost(
            **fields,
            validity_status="indicative",
            validity_reasons=["INSUFFICIENT_BOOK_DEPTH"],
        )
    meta = values[0]["raw"]
    try:
        fee, fee_version = archived_taker_fee(meta, estimate["curve"])
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return QuoteAndCost(
            **fields, validity_status="indicative", validity_reasons=["FEES_UNKNOWN"]
        )
    with localcontext(ACCOUNTING_CONTEXT):
        fields["fee_schedule_id"] = fee_version
        fields["costs"] = {
            "taker_fee_per_share": fee / requested_shares,
            "taker_fee_total": fee,
        }
    return QuoteAndCost(**fields, validity_status="executable_modeled", validity_reasons=[])


def apply_net_ev(
    cost: QuoteAndCost,
    market: CanonicalMarket,
    forecast: ForecastDisplay | None,
    requested_shares: Decimal | None,
    estimate_depth: Decimal | None,
    *,
    as_of: datetime,
) -> QuoteAndCost:
    """Add net EV only from a validated forecast, verified rules and an executable ask VWAP.

    Uses the existing #15 arithmetic. Anything missing leaves ``net_ev`` unknown.
    """
    from app.services.intelligence.binary_valuation import (
        BinaryValuationInput,
        value_binary_contract,
    )

    if (
        forecast is None
        or forecast.validity_status != "validated"
        or forecast.calibrated_probability is None
        or forecast.interval is None
        or cost.validity_status != "executable_modeled"
        or cost.vwap is None
        or cost.costs is None
        or cost.fee_schedule_id is None
        or requested_shares is None
        or estimate_depth is None
        or market.resolution.verification != "verified"
        or market.identity.selected_outcome is None
        or market.identity.selected_token_id is None
    ):
        return cost
    result = value_binary_contract(
        BinaryValuationInput(
            condition_id=market.identity.condition_id,
            token_id=market.identity.selected_token_id,
            outcome=market.identity.selected_outcome,
            side="BUY",
            shares=requested_shares,
            as_of=as_of,
            rules_hash=market.spec.source_hash,
            rule_review_status="verified",
            # Quantize to the valuation contract's 18 places, rounding against the user.
            calibrated_p_yes=forecast.calibrated_probability.quantize(_Q18),
            p_yes_lower=forecast.interval[0].quantize(_Q18, rounding=ROUND_FLOOR),
            p_yes_upper=forecast.interval[1].quantize(_Q18, rounding=ROUND_CEILING),
            executable_vwap=cost.vwap.quantize(_Q18, rounding=ROUND_CEILING),
            available_depth_shares=estimate_depth,
            taker_fee_per_share=cost.costs["taker_fee_per_share"].quantize(
                _Q18, rounding=ROUND_CEILING
            ),
            fee_schedule_version=cost.fee_schedule_id,
            market_implied_p=forecast.market_benchmark_p,
            forecast_id=forecast.forecast_id,
        )
    )
    if result.status != "valued":
        return cost.model_copy(
            update={
                "validity_status": "indicative",
                "validity_reasons": [*cost.validity_reasons, *result.abstain_reasons],
            }
        )
    return cost.model_copy(
        update={
            "net_ev": result.ev_net_per_share,
            "net_ev_lower_bound": result.ev_net_lower_bound,
            "break_even_probability": result.break_even_probability,
            "costs": {**(cost.costs or {}), "total_cost_per_share": result.total_cost_per_share},
        }
    )
