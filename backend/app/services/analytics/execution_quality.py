"""Read-only estimate persistence and shortfall reconciliation against settled orders (#7).

No order is created, signed, cancelled or submitted here. Estimates are matched to
account-owned settled gateway records only when exactly one order fits the quote window.
"""

import json
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from app.models import LiveOrder, UserTrade
from app.models.analytics.execution_estimate import ExecutionEstimateRecord
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import BinaryBook, MarketSpec
from app.services.market_data.book_research import ResearchQuery, load_analysis
from app.services.market_data.replay_capture import aware
from app.services.trading.execution.execution_estimator import (
    BPS,
    ExecutionEstimate,
    ShortfallInput,
    _quantile,
    compare_estimate_to_actual,
    implementation_shortfall,
)

MIN_SAMPLE_FOR_PERCENTILES = 20


def load_book(db, condition_id, outcome, now, max_age_seconds=60):
    """Latest as-of captured book for one token side, or (None, None, reason)."""
    result, _ = load_analysis(
        db,
        ResearchQuery(
            condition_id=condition_id,
            as_of=now,
            outcome=outcome,
            max_age_seconds=max_age_seconds,
        ),
    )
    if not result.get("spec"):
        return None, None, result.get("reason") or "NO_CAPTURED_BOOK"
    spec = MarketSpec.model_validate(result["spec"])
    return spec, BinaryBook.model_validate(result["books"][outcome]), None


def record_estimate(db, account, estimate: ExecutionEstimate, condition_id: str, now):
    row = ExecutionEstimateRecord(
        id=str(uuid4()),
        user_id=account.owner_user_id,
        account_id=account.id,
        estimate_id=estimate.estimate_id,
        order_intent_id=estimate.order_intent_id,
        condition_id=condition_id,
        token_id=estimate.token_id,
        side=estimate.side,
        mode=estimate.mode,
        status=estimate.status,
        shares=estimate.quantity,
        book_hash=estimate.book_snapshot_id,
        arrival_mid=estimate.arrival_mid,
        benchmark_at=estimate.benchmark_at,
        quote_expiry=estimate.quote_expiry,
        estimate_json=estimate.model_dump_json(),
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def _settled_fill(order, trade):
    """(filled_shares, vwap, fee) from an exchange-settled gateway record, else None."""
    response = order.response or {}
    if order.state not in {"FILLED", "CANCELLED"} or response.get("settled") is not True:
        return None
    try:
        shares = Decimal(str(response["size_matched"]))
        notional = Decimal(str(response["filled_notional"]))
    except (KeyError, ArithmeticError, ValueError):
        return None
    if not shares.is_finite() or not notional.is_finite() or shares < 0 or notional < 0:
        return None
    if shares > order.shares or (shares > 0 and not 0 < notional / shares < 1):
        return None
    fee = None
    if trade is not None and trade.fee_paid:  # 0 means "absent" in the legacy column
        fee = Decimal(str(trade.fee_paid))
    return shares, (notional / shares if shares > 0 else None), fee


def reconcile(db, account, now, window_days=30):
    """Match estimates to settled orders; report only what is observed."""
    since = now - timedelta(days=window_days)
    records = (
        db.query(ExecutionEstimateRecord)
        .filter(
            ExecutionEstimateRecord.user_id == account.owner_user_id,
            ExecutionEstimateRecord.account_id == account.id,
            ExecutionEstimateRecord.created_at >= since,
            ExecutionEstimateRecord.status == "modeled",
        )
        .order_by(ExecutionEstimateRecord.created_at)
        .all()
    )
    orders = (
        db.query(LiveOrder)
        .filter(
            LiveOrder.user_id == account.owner_user_id,
            LiveOrder.account_id == account.id,
            LiveOrder.created_at >= since,
        )
        .all()
    )
    candidates = {}
    for record in records:
        start, end = aware(record.created_at), aware(record.quote_expiry or record.created_at)
        candidates[record.id] = [
            o.id
            for o in orders
            if o.token_id == record.token_id
            and o.side == record.side
            and o.shares == record.shares
            and start <= aware(o.created_at) <= end
        ]
    claims = {}
    for oids in candidates.values():
        for oid in oids:
            claims[oid] = claims.get(oid, 0) + 1
    by_id = {o.id: o for o in orders}
    rows, errors, fills, unmatched, ambiguous, unverified = [], [], [], 0, 0, 0
    with localcontext(ACCOUNTING_CONTEXT):
        for record in records:
            oids = candidates[record.id]
            if not oids:
                unmatched += 1
                continue
            if len(oids) > 1 or claims[oids[0]] > 1:
                ambiguous += 1
                continue
            order = by_id[oids[0]]
            trade = db.get(UserTrade, order.trade_id)
            settled = _settled_fill(order, trade)
            if settled is None:
                unverified += 1
                continue
            shares, vwap, fee = settled
            estimate = ExecutionEstimate.model_validate(json.loads(record.estimate_json))
            actual = implementation_shortfall(
                ShortfallInput(
                    side=record.side,
                    ordered_shares=order.shares,
                    filled_shares=shares,
                    fill_vwap=vwap,
                    fees_paid=fee,
                    arrival_mid=record.arrival_mid,
                    benchmark_at=aware(record.benchmark_at),
                )
            )
            gap = compare_estimate_to_actual(estimate, actual)
            fills.append(actual.fill_rate)
            if gap.status == "compared":
                errors.append(gap.error_bps)
            base = record.arrival_mid * order.shares
            rows.append(
                {
                    "estimate_record_id": record.id,
                    "trade_id": order.trade_id,
                    "order_hash": order.order_hash,
                    "benchmark_at": aware(record.benchmark_at).isoformat(),
                    "arrival_mid": str(record.arrival_mid),
                    "estimated_vwap": str(estimate.executable_vwap),
                    "actual_vwap": None if vwap is None else str(vwap),
                    "fill_rate": str(actual.fill_rate),
                    "gap": gap.model_dump(mode="json"),
                    # Cost waterfall in quote currency versus the same arrival midpoint.
                    "waterfall": {
                        "benchmark_notional": str(base),
                        "estimated_price_cost": None
                        if estimate.executable_vwap is None
                        else str(estimate.expected_cost_vs_mid_bps * base / BPS),
                        "actual_price_cost": str(actual.execution_cost),
                        "fee": None if actual.fee_cost is None else str(actual.fee_cost),
                        "missed_trade_cost": None
                        if actual.missed_trade_cost is None
                        else str(actual.missed_trade_cost),
                        "components_missing": actual.components_missing,
                    },
                }
            )
        ordered = sorted(errors)
        enough = len(ordered) >= MIN_SAMPLE_FOR_PERCENTILES
        summary = {
            "estimates_modeled": len(records),
            "matched_settled": len(rows),
            "unmatched_estimates": unmatched,
            "ambiguous_estimates": ambiguous,
            "matched_but_not_settled": unverified,
            "mean_fill_rate": str(sum(fills, Decimal(0)) / len(fills)) if fills else None,
            "mean_error_bps": str(sum(ordered, Decimal(0)) / len(ordered)) if ordered else None,
            "p50_error_bps": str(_quantile(ordered, Decimal("0.5"))) if enough else None,
            "p95_error_bps": str(_quantile(ordered, Decimal("0.95"))) if enough else None,
            "percentile_min_sample": MIN_SAMPLE_FOR_PERCENTILES,
        }
    return {
        "version": "execution-quality-v1",
        "window_days": window_days,
        "summary": summary,
        "rows": rows,
        "matching_rule": (
            "same account, token, side and share count; created inside the estimate's quote "
            "window; exactly one order and one estimate may claim each other"
        ),
        "fees_note": "actual fees appear only when the trade recorded a positive fee",
        "profitability_claim": False,
    }
