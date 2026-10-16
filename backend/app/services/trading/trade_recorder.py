"""Shared UserTrade recorder (plan 03 Phase A).

Every strategy that reaches the CLOB must leave an attributable
``UserTrade`` row: ``strategy_source`` drives per-strategy P&L
(``pnl_reconciliation`` is the only writer of ``UserTrade.pnl``) and
the execution-analytics endpoints. Before this helper each strategy
built its own row, which is why the market maker omitted the strategy,
the inverse bot wrote no row at all, and the stop-loss/take-profit
closes never reached the FIFO matcher.

Contract
--------
``record_user_trade`` builds and adds the row, then flushes so the
caller's ``commit`` (or rollback) decides whether the trade is
durable. It never raises: a recording failure is logged and returns
``None`` so the money path is never blocked by telemetry.

Callers own the surrounding transaction, so a rollback still removes
the trade — correct semantics, since an order that never committed
should not be reported as executed.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.trading.user_trade import UserTrade

# Canonical vocabulary (also used to validate the
# /api/trades/analytics/trades filter) lives in execution_analytics;
# re-exported here so callers can import both from one module.
from app.services.analytics.execution_analytics import STRATEGY_SOURCES  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = ["STRATEGY_SOURCES", "commit_recorded_trade", "record_user_trade"]


def commit_recorded_trade(db: Session) -> None:
    """Commit a recorded trade together with its audit event."""
    db.commit()


def record_user_trade(
    db: Session,
    *,
    user_id: int,
    market_id: str,
    action: str,
    amount: float,
    price: float,
    status: str,
    strategy_source: str,
    token_id: str | None = None,
    order_hash: str | None = None,
    expected_price: float | None = None,
    expected_size: float | None = None,
    pnl: float | None = None,
    **extra: Any,
) -> UserTrade | None:
    """Record one strategy's trade, or return ``None`` on failure.

    Args:
        db: Session owned by the caller; this function only adds and
            flushes.
        user_id: Owning user.
        market_id: Condition id (or token id when no market id is known).
        action: ``buy`` or ``sell``.
        amount: USDC notional for buys, shares for sells (matching the
            semantics already used by the call sites).
        price: Limit/reference price of the order.
        status: ``pending`` (submitted, awaiting fill), ``executed``,
            ``failed``, ``rejected`` or ``simulated``.
        strategy_source: One of :data:`STRATEGY_SOURCES`.
        token_id: CLOB token id.
        order_hash: CLOB order hash when the order was accepted.
        expected_price/expected_size: Quoted price/size at submission,
            the basis for the slippage analytics.
        pnl: Realized P&L when the caller already knows it (paper
            paths); live P&L is written only by
            ``app.services.analytics.pnl_reconciliation``.
        **extra: Additional ``UserTrade`` columns (e.g.
            ``copied_from_wallet``).

    Returns:
        The pending/added row, or ``None`` if recording failed.
    """
    try:
        row = UserTrade(
            user_id=user_id,
            market_id=market_id,
            token_id=token_id,
            action=action,
            amount=amount,
            price=price,
            status=status,
            order_hash=order_hash,
            expected_price=expected_price,
            expected_size=expected_size,
            strategy_source=strategy_source,
            pnl=pnl,
            **extra,
        )
        db.add(row)
        db.flush()
        # The audit event is appended on the same session, inside the
        # caller's transaction, so a rollback removes both the trade
        # and the description of it (plan 10 Phase A).
        from app.services.platform.event_log import TRADE_EVENT_BY_STATUS, append

        append(
            db,
            event_type=TRADE_EVENT_BY_STATUS.get(status, "order_submitted"),
            aggregate_type="order",
            aggregate_id=order_hash or f"{strategy_source}:{market_id}",
            user_id=user_id,
            payload={
                "strategy_source": strategy_source,
                "action": action,
                "amount": amount,
                "price": price,
                "status": status,
                "market_id": market_id,
                "trade_id": row.id,
            },
            source="trade_recorder",
        )
        return row
    except Exception as exc:
        # Telemetry must never break the money path. The caller's
        # transaction decides durability; a flush failure is logged and
        # swallowed here so the order itself still proceeds.
        logger.error(
            "Failed to record %s trade for user=%s market=%s: %s",
            strategy_source,
            user_id,
            str(market_id)[:20],
            exc,
        )
        return None
