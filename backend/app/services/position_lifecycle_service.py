"""
Position Lifecycle Manager — reporting on positions across their lifecycle.

Every five minutes this service walks each user's trades and reports:

  1. **Realized PnL** — reconciles sell trades against open buy lots (FIFO) and
     writes ``UserTrade.pnl``. Every loss control in
     ``copy_trade_service._apply_global_safety_caps`` reads that column, so this
     step is what makes the daily-loss, monthly-loss, drawdown and total-loss
     circuit breakers functional rather than permanently zero.
  2. **Resolved markets** — positions on markets that have settled and appear
     to still be redeemable. On Polymarket, unsettled winning shares must be
     redeemed on-chain; leaving them unclaimed forfeits the payout, so this is
     surfaced prominently for operator or user action.
  3. **Stale positions** — markets that are *still open* and whose oldest open
     lot is older than ``STALE_POSITION_HOURS``. Buys and sells are netted, so a
     position closed long ago is never reported.
  4. **Duplicate positions** — markets entered more than once, with a
     weighted-average entry price.

Note on scope: steps 2-4 **detect and report only**. This module does not submit
redemption or closing transactions. ``AUTO_REDEEM_ENABLED`` exists for metrics
compatibility but no redemption is performed here; see
``get_lifecycle_metrics`` and the README for the intended follow-up.

Inspired by dexorynlabs/polymarket-agents position management.
"""

import asyncio
import contextlib
import logging
import math
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.services.pnl_reconciliation import reconcile_realized_pnl
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

_lifecycle_task: asyncio.Task | None = None

# Default configuration
LIFECYCLE_CHECK_INTERVAL = 300  # 5 minutes
STALE_POSITION_HOURS = 168  # 7 days
AUTO_REDEEM_ENABLED = True


def _to_float(value: Any, default: float = 0.0) -> float | None:
    """Coerce an upstream API value to a finite float.

    The Polymarket API occasionally returns ``None``, an empty string or a
    non-numeric token where a number is expected. Any such value must not be
    allowed to abort processing of the remaining positions.

    Args:
        value: Raw value from the upstream payload.
        default: Value substituted for ``None`` or an empty string.

    Returns:
        The parsed float, or ``None`` when the value is not a usable number.
    """
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and +/-inf are not usable position sizes.
    return parsed if math.isfinite(parsed) else None


def _parse_resolved_position(pos: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    """Turn one raw upstream position into a resolved-position record.

    Isolated from the caller so that one malformed entry is skipped instead of
    discarding every position already collected for the user.

    Args:
        pos: Raw position dict from the Polymarket positions endpoint.
        user_id: Owner of the position, used only for logging.

    Returns:
        A resolved-position record, or ``None`` when the entry is not a
        redeemable resolved position (missing market, unresolved, non-positive
        or unparseable size).
    """
    condition_id = pos.get("conditionId") or pos.get("condition_id", "")
    market_data = pos.get("market") or {}
    if not market_data:
        return None

    # A market is settled once it is flagged resolved or closed.
    if not (market_data.get("resolved") or market_data.get("closed")):
        return None

    size = _to_float(pos.get("size"))
    if size is None:
        logger.warning(
            "Skipping resolved position for user %d: unparseable size %r (condition=%s)",
            user_id,
            pos.get("size"),
            str(condition_id)[:20],
        )
        return None
    if size <= 0:
        return None

    return {
        "condition_id": condition_id,
        "title": market_data.get("question", market_data.get("title", "Unknown")),
        "size": size,
        "resolved": True,
        "outcome_prices": market_data.get("outcomePrices", []),
    }


async def _check_resolved_markets(
    db: Session, user_id: int, wallet_address: str
) -> list[dict[str, Any]]:
    """Find resolved markets where the user still holds a redeemable position.

    Each position is parsed independently, so a single malformed upstream entry
    cannot discard the rest of the user's positions. A transport-level failure
    still yields an empty list, which is logged.

    Args:
        db: Open database session, retained for signature compatibility.
        user_id: Owner of the positions.
        wallet_address: Wallet to query upstream.

    Returns:
        Resolved-position records that appear to be awaiting redemption.
    """
    from app.services.polymarket_service import get_polymarket_service

    redeemed: list[dict[str, Any]] = []
    try:
        service = get_polymarket_service()
        positions = await service.get_positions(wallet_address)
    except Exception as exc:
        logger.error("Error fetching positions for user %d: %s", user_id, exc)
        return redeemed

    for pos in positions:
        if not isinstance(pos, dict):
            logger.warning(
                "Skipping non-dict position entry for user %d: %r", user_id, type(pos).__name__
            )
            continue

        record = _parse_resolved_position(pos, user_id)
        if record is None:
            continue

        redeemed.append(record)
        logger.info(
            "Resolved position found for user %d: %s (condition=%s, size=%.2f)",
            user_id,
            record["title"],
            str(record["condition_id"])[:20],
            record["size"],
        )

    return redeemed


async def _check_stale_positions(
    db: Session, user_id: int, wallet_address: str, max_age_hours: int = STALE_POSITION_HOURS
) -> list[dict[str, Any]]:
    """Identify positions still open and held longer than the threshold.

    Buys and sells are netted per market so that a position which was fully
    closed long ago is not reported as stale. Only markets whose earliest open
    buy predates the cutoff, and which still have a non-zero net amount, are
    returned.

    Args:
        db: Open database session.
        user_id: Owner whose trades should be examined.
        wallet_address: Unused; retained so both lifecycle probes share a
            signature and the caller can pass the user uniformly.
        max_age_hours: Age after which a still-open position counts as stale.

    Returns:
        One record per stale market, with the age of the oldest open lot.
    """
    from app.models.user_trade import UserTrade

    stale: list[dict[str, Any]] = []
    cutoff = utc_now() - timedelta(hours=max_age_hours)

    try:
        # Net position per market: buys add, sells subtract. Filtering to the
        # executed/cancelled-free set keeps failed and pending orders from
        # distorting the open size.
        rows = (
            db.query(
                UserTrade.market_id,
                UserTrade.action,
                UserTrade.executed_at,
            )
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.action.in_(("buy", "sell")),
            )
            .order_by(UserTrade.executed_at.asc())
            .all()
        )
    except Exception as exc:
        logger.error("Error checking stale positions for user %d: %s", user_id, exc)
        return stale

    # Walk trades oldest-first, consuming open lots as sells arrive.
    net_amount: dict[str, float] = {}
    first_open_at: dict[str, Any] = {}
    trade_count: dict[str, int] = {}

    for market_id, action, executed_at in rows:
        if executed_at is None:
            continue
        net_amount.setdefault(market_id, 0.0)
        if action == "buy":
            if net_amount[market_id] == 0.0:
                first_open_at[market_id] = executed_at
                trade_count[market_id] = 0
            net_amount[market_id] += 1.0
            trade_count[market_id] += 1
        else:
            net_amount[market_id] = max(0.0, net_amount[market_id] - 1.0)
            if net_amount[market_id] == 0.0:
                # Fully closed: drop the tracking state so a later buy starts
                # a fresh lot with its own age.
                first_open_at.pop(market_id, None)

    for market_id, amount_open in net_amount.items():
        if amount_open <= 0:
            continue
        opened_at = first_open_at.get(market_id)
        if opened_at is None or opened_at >= cutoff:
            continue
        age_hours = (utc_now() - opened_at).total_seconds() / 3600
        stale.append(
            {
                "market_id": market_id,
                "open_lots": amount_open,
                "open_trades": trade_count.get(market_id, 0),
                "age_hours": round(age_hours, 1),
                "opened_at": opened_at.isoformat(),
            }
        )

    return stale


async def _merge_duplicate_positions(db: Session, user_id: int) -> list[dict[str, Any]]:
    """Detect duplicate positions on the same market outcome and merge them.

    Inspired by poly_merger — finds multiple trades on the same token_id
    and computes a weighted-average entry price.
    """
    from sqlalchemy import func

    from app.models.user_trade import UserTrade

    merged: list[dict[str, Any]] = []

    try:
        # Find market_ids with multiple buy trades
        dupes = (
            db.query(
                UserTrade.market_id,
                func.count(UserTrade.id).label("count"),
                func.sum(UserTrade.amount * UserTrade.price).label("total_cost"),
                func.sum(UserTrade.amount).label("total_amount"),
            )
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.action == "buy",
            )
            .group_by(UserTrade.market_id)
            .having(func.count(UserTrade.id) > 1)
            .all()
        )

        for dupe in dupes:
            avg_price = (dupe.total_cost / dupe.total_amount) if dupe.total_amount > 0 else 0
            merged.append(
                {
                    "market_id": dupe.market_id,
                    "trade_count": dupe.count,
                    "total_amount": round(float(dupe.total_amount), 4),
                    "avg_entry_price": round(float(avg_price), 4),
                    "total_cost": round(float(dupe.total_cost), 4),
                }
            )

    except Exception as e:
        logger.error("Error detecting duplicate positions for user %d: %s", user_id, e)

    return merged


async def run_lifecycle_check(user_id: int, wallet_address: str) -> dict[str, Any]:
    """Run a full lifecycle check for a user.

    Also reconciles realized PnL. This must happen before the safety caps are
    consulted anywhere: `_apply_global_safety_caps` derives the daily/monthly
    loss, drawdown and total-loss layers from ``UserTrade.pnl``, and that column
    is only ever written by the reconciler. Skipping it leaves every loss
    circuit breaker reading a permanent zero.

    Returns:
        Summary of resolved, stale and duplicate positions plus the PnL result.
    """
    db = SessionLocal()
    try:
        pnl_summary = reconcile_realized_pnl(db, user_id)

        resolved = await _check_resolved_markets(db, user_id, wallet_address)
        stale = await _check_stale_positions(db, user_id, wallet_address)
        duplicates = await _merge_duplicate_positions(db, user_id)

        return {
            "user_id": user_id,
            "checked_at": utc_now().isoformat(),
            "resolved_positions": resolved,
            "resolved_count": len(resolved),
            "stale_positions": stale,
            "stale_count": len(stale),
            "duplicate_positions": duplicates,
            "duplicate_count": len(duplicates),
            "pnl": {
                "sells_processed": pnl_summary.sells_processed,
                "realized_pnl": round(pnl_summary.realized_pnl, 4),
                "unmatched_sell_shares": round(pnl_summary.unmatched_sell_shares, 4),
                "open_lots_remaining": pnl_summary.open_lots_remaining,
            },
        }
    finally:
        db.close()


async def _lifecycle_loop():
    """Background loop: periodically check positions for all users."""
    if not acquire_scheduler_lock("position_lifecycle"):
        return
    logger.info("Position lifecycle manager started (interval=%ds)", LIFECYCLE_CHECK_INTERVAL)

    try:
        while True:
            try:
                db = SessionLocal()
                try:
                    from app.models.user import User

                    users = db.query(User).all()
                    for user in users:
                        try:
                            result = await run_lifecycle_check(user.id, user.wallet_address)
                            total = (
                                result["resolved_count"]
                                + result["stale_count"]
                                + result["duplicate_count"]
                            )
                            if total > 0:
                                logger.info(
                                    "Lifecycle check for user %d: %d resolved, "
                                    "%d stale, %d duplicates",
                                    user.id,
                                    result["resolved_count"],
                                    result["stale_count"],
                                    result["duplicate_count"],
                                )
                        except Exception as e:
                            logger.error("Lifecycle check failed for user %d: %s", user.id, e)
                finally:
                    db.close()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Position lifecycle loop error: %s", e)

            scheduler_heartbeat("position_lifecycle")
            await asyncio.sleep(LIFECYCLE_CHECK_INTERVAL)
    finally:
        release_scheduler_lock("position_lifecycle")

    logger.info("Position lifecycle manager stopped")


async def start_position_lifecycle_manager():
    """Start the position lifecycle background task."""
    global _lifecycle_task
    if _lifecycle_task and not _lifecycle_task.done():
        logger.warning("Position lifecycle manager already running")
        return
    _lifecycle_task = asyncio.create_task(_lifecycle_loop())
    logger.info("Position lifecycle manager started")


async def stop_position_lifecycle_manager():
    """Stop the position lifecycle background task."""
    global _lifecycle_task
    if _lifecycle_task and not _lifecycle_task.done():
        _lifecycle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _lifecycle_task
    _lifecycle_task = None
    logger.info("Position lifecycle manager stopped")


def get_lifecycle_metrics() -> dict[str, Any]:
    """Return running status and effective thresholds.

    ``auto_redeem_enabled`` is reported for metrics compatibility only: this
    module does not perform redemption (see the module docstring).
    """
    return {
        "running": _lifecycle_task is not None and not _lifecycle_task.done(),
        "check_interval_seconds": LIFECYCLE_CHECK_INTERVAL,
        "stale_threshold_hours": STALE_POSITION_HOURS,
        "auto_redeem_enabled": AUTO_REDEEM_ENABLED,
        "detects_resolved_markets": True,
        "reports_stale_positions": True,
        "reports_duplicate_positions": True,
    }
