"""Realized PnL reconciliation.

Every loss control in the bot reads ``UserTrade.pnl``: the daily loss limit, the
monthly loss limit, the maximum-drawdown layer and the total-loss halt in
``copy_trade_service._apply_global_safety_caps``. Nothing in the codebase ever
wrote that column, so all four layers evaluated against a permanent zero and the
bot had **no working loss circuit breaker**.

This module closes that gap. It walks a user's trades oldest-first, matches
sells against open buy lots with FIFO, and writes realized profit/loss onto the
closing sell row.

Design notes
------------
- **Idempotent.** Sells that already carry a ``pnl`` value are skipped, so the
  reconciler can run on every monitoring cycle without double-counting.
- **FIFO.** Longest-held lots are consumed first, which matches how the
  position ledger presents cost basis elsewhere.
- **USDC-denominated.** ``UserTrade.amount`` is a USDC notional and ``price`` is
  the execution price, so share quantity is ``amount / price``.
- **Partial closes** allocate PnL pro-rata across the lots they consume.
- Anything that cannot be interpreted (zero/negative price, missing timestamp)
  is skipped and logged rather than raising, because a single malformed row must
  not leave the risk controls without data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.user_trade import UserTrade
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Only these statuses represent a trade that actually reached the exchange.
# Anything pending/failed/cancelled must never contribute to realised PnL.
_REALISED_STATUSES = ("executed", "filled")


@dataclass
class OpenLot:
    """A remaining open buy lot, consumed FIFO by later sells."""

    trade_id: int
    remaining_shares: float
    entry_price: float


@dataclass
class ReconciliationResult:
    """Outcome of reconciling one user's trades."""

    sells_processed: int = 0
    lots_consumed: int = 0
    realized_pnl: float = 0.0
    unmatched_sell_shares: float = 0.0
    open_lots_remaining: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def _shares_for(trade: UserTrade) -> float | None:
    """Convert a trade's USDC notional into shares.

    Returns ``None`` when the price or amount is unusable, so the caller can skip
    the row instead of dividing by zero or inventing a position.
    """
    try:
        price = float(trade.price)
        amount = float(trade.amount)
    except (TypeError, ValueError):
        return None
    if price <= 0 or amount <= 0:
        return None
    return amount / price


def reconcile_realized_pnl(db: Session, user_id: int) -> ReconciliationResult:
    """Write realized PnL onto sell trades that close (part of) a buy position.

    Args:
        db: Open database session.
        user_id: Owner whose trades should be reconciled.

    Returns:
        A summary of what was reconciled, including realized PnL for the call.
    """
    result = ReconciliationResult()

    trades = (
        db.query(UserTrade)
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.action.in_(("buy", "sell")),
            UserTrade.executed_at.isnot(None),
        )
        .order_by(UserTrade.executed_at.asc(), UserTrade.id.asc())
        .all()
    )

    open_lots: dict[str, list[OpenLot]] = {}

    for trade in trades:
        status = (trade.status or "").lower()
        if status not in _REALISED_STATUSES:
            result.skipped += 1
            continue

        shares = _shares_for(trade)
        if shares is None:
            result.skipped += 1
            result.warnings.append(f"trade {trade.id}: unusable price/amount")
            continue

        market_id = trade.market_id or ""

        if trade.action == "buy":
            open_lots.setdefault(market_id, []).append(
                OpenLot(
                    trade_id=trade.id,
                    remaining_shares=shares,
                    entry_price=float(trade.price),
                )
            )
            continue

        # ── Sell: match against open lots ──────────────────────────────
        if trade.pnl is not None:
            # Already reconciled by a previous pass.
            continue

        lots = open_lots.get(market_id, [])
        if not lots:
            # A sell with no recorded buy: cannot compute a basis. Flag it so
            # the gap is visible instead of silently treating it as break-even.
            result.unmatched_sell_shares += shares
            result.warnings.append(f"sell {trade.id} on {market_id[:20]}: no open buy lot matched")
            continue

        sell_price = float(trade.price)
        remaining_to_close = shares
        realized = 0.0
        consumed = 0

        for lot in lots:
            if remaining_to_close <= 1e-9:
                break
            take = min(lot.remaining_shares, remaining_to_close)
            realized += (sell_price - lot.entry_price) * take
            lot.remaining_shares -= take
            remaining_to_close -= take
            consumed += 1

        # Drop fully consumed lots so later sells cannot reuse them.
        open_lots[market_id] = [lot for lot in lots if lot.remaining_shares > 1e-9]

        # Any sell quantity beyond the available basis is unmatched; it still
        # contributes PnL for the portion that did match.
        if remaining_to_close > 1e-9:
            result.unmatched_sell_shares += remaining_to_close
            result.warnings.append(
                f"sell {trade.id} on {market_id[:20]}: {remaining_to_close:.4f} shares "
                "exceeded the recorded buy lots"
            )

        trade.pnl = round(realized, 6)
        trade.updated_at = utc_now()
        result.sells_processed += 1
        result.lots_consumed += consumed
        result.realized_pnl += realized

    result.open_lots_remaining = sum(len(v) for v in open_lots.values())

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    if result.sells_processed or result.warnings:
        logger.info(
            "PnL reconciliation user=%d: %d sells, %d lots consumed, "
            "realized=%.4f, unmatched=%.4f shares, %d still open",
            user_id,
            result.sells_processed,
            result.lots_consumed,
            result.realized_pnl,
            result.unmatched_sell_shares,
            result.open_lots_remaining,
        )
    for warning in result.warnings[:5]:
        logger.warning("PnL reconciliation user=%d: %s", user_id, warning)

    return result


def total_realized_pnl(db: Session, user_id: int) -> float:
    """Sum of every recorded realized PnL for a user.

    Returns 0.0 when nothing has been reconciled yet, which is why the loss
    controls must not treat a zero as "no loss occurred" without also checking
    whether any PnL data exists at all.
    """
    from sqlalchemy import func

    value = (
        db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0))
        .filter(UserTrade.user_id == user_id, UserTrade.pnl.isnot(None))
        .scalar()
    )
    return float(value or 0.0)


def has_pnl_history(db: Session, user_id: int) -> bool:
    """Whether any PnL value has ever been recorded for this user.

    Used to distinguish "the user has not lost money" from "we have no data",
    which are very different states for a circuit breaker.
    """
    count = (
        db.query(UserTrade.id)
        .filter(UserTrade.user_id == user_id, UserTrade.pnl.isnot(None))
        .limit(1)
        .count()
    )
    return count > 0


def unreconciled_sell_count(db: Session, user_id: int) -> int:
    """Number of realised sells still awaiting a ``pnl`` value.

    A persistently non-zero result means the reconciler is not keeping up.
    """
    return (
        db.query(UserTrade)
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.action == "sell",
            UserTrade.status.in_(_REALISED_STATUSES),
            UserTrade.pnl.is_(None),
        )
        .count()
    )


def reconciliation_summary(db: Session, user_id: int) -> dict[str, Any]:
    """Compact health view of a user's PnL bookkeeping, for admin endpoints."""
    return {
        "user_id": user_id,
        "total_realized_pnl": round(total_realized_pnl(db, user_id), 4),
        "has_pnl_history": has_pnl_history(db, user_id),
        "unreconciled_sells": unreconciled_sell_count(db, user_id),
    }
