"""Read application-recorded fills/reservations; no wallet credentials or mutations.

This scope excludes unrecorded external wallet activity. It is an advisory bound
on recorded executions, not a claim that the complete wallet is reconciled.
Unknown fills, cancellations, revisions and lifecycle states abstain explicitly.
"""

from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, localcontext

from sqlalchemy import select

from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.services.analytics.event_correlation_risk import EventCorrelationRequest, ExposureLeg
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.intelligence.resolution_rules import get_rule_snapshot_as_of


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def recorded_exposure(db, account, as_of=None):
    with localcontext(ACCOUNTING_CONTEXT):
        return _recorded_exposure(db, account, as_of)


def _recorded_exposure(db, account, as_of=None):
    now = as_of or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("ABSTAIN_NAIVE_TIMESTAMP")
    trades = db.scalars(
        select(UserTrade)
        .where(
            UserTrade.user_id == account.owner_user_id,
            UserTrade.account_id == account.id,
        )
        .order_by(UserTrade.created_at, UserTrade.id)
        .limit(501)
    ).all()
    if len(trades) > 500:
        raise ValueError("ABSTAIN_RECORDED_PORTFOLIO_BOUND_EXCEEDED")
    orders = db.scalars(
        select(LiveOrder)
        .where(
            LiveOrder.user_id == account.owner_user_id,
            LiveOrder.account_id == account.id,
        )
        .limit(501)
    ).all()
    if len(orders) > 500 or any(order.trade_id not in {t.id for t in trades} for order in orders):
        raise ValueError("ABSTAIN_INCOMPLETE_ORDER_TRADE_JOIN")
    order_map = {order.trade_id: order for order in orders}
    lots, pending, rules, sources = {}, [], {}, []
    sold = {}
    for trade in trades:
        if aware(trade.created_at) > now or (trade.updated_at and aware(trade.updated_at) > now):
            raise ValueError("ABSTAIN_HISTORICAL_STATE_NOT_RECORDED")
        if trade.status in {"failed", "cancelled"} and not trade.filled_size:
            order = order_map.get(trade.id)
            if order is not None and order.state not in {"REJECTED", "CANCELLED"}:
                raise ValueError("ABSTAIN_ORDER_CANCELLATION_NOT_RECONCILED")
            if (
                order is not None
                and order.state == "CANCELLED"
                and "size_matched" not in order.response
            ):
                raise ValueError("ABSTAIN_CANCELLATION_RECEIPT_MISSING")
            if order is not None and Decimal(str(order.response.get("size_matched", 0))) != 0:
                raise ValueError("ABSTAIN_CANCELLED_BUT_UNCONFIRMED_FILL")
            continue
        cid = str(trade.market_id).lower()
        if cid not in rules:
            rule = get_rule_snapshot_as_of(db, cid, now)
            if rule is None or rule.review_status != "verified":
                raise ValueError("ABSTAIN_UNVERIFIED_POSITION_RULE")
            if (now - aware(rule.observed_at)).total_seconds() > 300:
                raise ValueError("ABSTAIN_STALE_POSITION_RULE")
            rules[cid] = rule
        rule = rules[cid]
        mapping = {token: label for label, token in rule.token_outcomes.items()}
        if trade.token_id not in mapping:
            raise ValueError("ABSTAIN_POSITION_TOKEN_RULE_MISMATCH")
        outcome = mapping[trade.token_id]
        common = {
            "condition_id": cid,
            "token_id": trade.token_id,
            "outcome": outcome,
            "rule_hash": rule.source_hash,
            "oracle": rule.oracle,
            "underlying_asset": rule.oracle_symbol.split("/")[0] if rule.oracle_symbol else None,
            "resolution_utc": rule.cutoff_utc,
            "event_id": str(rule.raw_market.get("event_id") or "") or None,
        }
        if trade.status == "executed" and (trade.filled_size is None or trade.filled_size <= 0):
            raise ValueError("ABSTAIN_CONFIRMED_FILL_QUANTITY_MISSING")
        filled = Decimal(str(trade.filled_size or 0))
        if not filled.is_finite() or filled < 0:
            raise ValueError("ABSTAIN_INVALID_RECORDED_FILL")
        side = trade.action.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("ABSTAIN_UNSUPPORTED_RECORDED_LIFECYCLE")
        if filled:
            if trade.filled_price is None or trade.fee_paid is None or trade.executed_at is None:
                raise ValueError("ABSTAIN_INCOMPLETE_CONFIRMED_FILL")
            if aware(trade.executed_at) > now:
                raise ValueError("ABSTAIN_FUTURE_FILL")
            price, fee = (
                Decimal(str(trade.filled_price)),
                (Decimal(str(trade.fee_paid)) / filled).quantize(
                    Decimal("1e-18"), rounding=ROUND_CEILING
                ),
            )
            if side == "BUY":
                lots.setdefault(trade.token_id, []).append(
                    ExposureLeg(
                        **common,
                        leg_id=f"trade:{trade.id}",
                        shares=filled,
                        entry_price=price,
                        fee_per_share=fee,
                    )
                )
            else:
                sold[trade.token_id] = sold.get(trade.token_id, Decimal(0)) + filled
        order = order_map.get(trade.id)
        if order is not None and (not order.reserved.is_finite() or order.reserved < 0):
            raise ValueError("ABSTAIN_INVALID_DURABLE_RESERVATION")
        if order is None:
            if trade.status != "executed":
                raise ValueError("ABSTAIN_PENDING_ORDER_WITHOUT_DURABLE_QUANTITY")
        else:
            if (
                aware(order.updated_at) > now
                or order.token_id != trade.token_id
                or order.side != side
            ):
                raise ValueError("ABSTAIN_ORDER_IDENTITY_OR_CLOCK_MISMATCH")
            if not order.shares.is_finite() or filled > order.shares:
                raise ValueError("ABSTAIN_INCONSISTENT_FILLED_QUANTITY")
            if order.state in {"SUBMITTING", "UNKNOWN", "SUBMITTED", "PARTIALLY_FILLED", "FILLED"}:
                unfilled = order.shares - filled
                if unfilled:
                    if side != "BUY":
                        raise ValueError("ABSTAIN_PENDING_SELL_HEDGE_UNCERTAINTY")
                    # Use a maximal per-share cost bound while fee/fill economics
                    # remain unknown. No pending order supplies a profitable hedge.
                    pending.append(
                        ExposureLeg(
                            **common,
                            leg_id=f"order:{order.id}",
                            shares=unfilled,
                            entry_price=order.price,
                            fee_per_share=Decimal(1),
                            status="pending_order",
                        )
                    )
            elif order.state not in {"CANCELLED", "REJECTED"}:
                raise ValueError("ABSTAIN_UNKNOWN_DURABLE_ORDER_STATE")
        sources.append(
            {
                "trade_id": trade.id,
                "updated_at": aware(trade.updated_at or trade.created_at).isoformat(),
                "order_id": order.id if order else None,
                "reserved_principal": str(order.reserved) if order else None,
                "rule_hash": rule.source_hash,
            }
        )
    for token, quantity in sold.items():
        remaining = quantity
        # Aggregated fill rows do not prove lot-selection chronology. Retain
        # highest-cost recorded lots so the remaining cost basis is an upper bound.
        for lot in sorted(
            lots.get(token, []), key=lambda item: item.entry_price + item.fee_per_share
        ):
            take = min(remaining, lot.shares)
            lot.shares -= take
            remaining -= take
        if remaining:
            raise ValueError("ABSTAIN_UNRECONCILED_SELL_INVENTORY")
    legs = [lot for group in lots.values() for lot in group if lot.shares > 0] + pending
    # Missing cross-condition relations grant no diversification. The scenario
    # primitive can analyze declared hypotheses separately; account reads never
    # accept an LLM/caller's relation as a verified hedge.
    return EventCorrelationRequest(legs=legs, as_of=now), sources
