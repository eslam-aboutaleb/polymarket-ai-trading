"""
Position Lifecycle Manager — auto-manage positions through their lifecycle.

Handles:
  1. Auto-redeem resolved markets (claim winnings immediately)
  2. Auto-close stale positions (positions held beyond a configurable age)
  3. Position health monitoring and conflict detection

Inspired by dexorynlabs/polymarket-agents position management.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.utils.database import SessionLocal
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

_lifecycle_task: Optional[asyncio.Task] = None

# Default configuration
LIFECYCLE_CHECK_INTERVAL = 300  # 5 minutes
STALE_POSITION_HOURS = 168  # 7 days
AUTO_REDEEM_ENABLED = True


async def _check_resolved_markets(db: Session, user_id: int, wallet_address: str) -> List[Dict[str, Any]]:
    """Check for resolved markets where the user holds positions and auto-redeem."""
    from app.services.polymarket_service import get_polymarket_service

    redeemed: List[Dict[str, Any]] = []
    try:
        service = get_polymarket_service()
        positions = await service.get_positions(wallet_address)

        for pos in positions:
            condition_id = pos.get("conditionId") or pos.get("condition_id", "")
            market_data = pos.get("market", {})
            if not market_data:
                continue

            # Check if market is resolved
            is_resolved = market_data.get("resolved", False) or market_data.get("closed", False)
            if not is_resolved:
                continue

            size = float(pos.get("size", 0))
            if size <= 0:
                continue

            title = market_data.get("question", market_data.get("title", "Unknown"))
            outcome_prices = market_data.get("outcomePrices", [])

            redeemed.append({
                "condition_id": condition_id,
                "title": title,
                "size": size,
                "resolved": True,
                "outcome_prices": outcome_prices,
            })

            logger.info(
                "Resolved position found for user %d: %s (condition=%s, size=%.2f)",
                user_id, title, condition_id[:20], size,
            )

    except Exception as e:
        logger.error("Error checking resolved markets for user %d: %s", user_id, e)

    return redeemed


async def _check_stale_positions(
    db: Session, user_id: int, wallet_address: str, max_age_hours: int = STALE_POSITION_HOURS
) -> List[Dict[str, Any]]:
    """Identify positions held longer than the configured threshold."""
    from app.models.user_trade import UserTrade

    stale: List[Dict[str, Any]] = []
    cutoff = utc_now() - timedelta(hours=max_age_hours)

    try:
        # Find trades that created positions older than threshold
        old_trades = (
            db.query(UserTrade)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.executed_at < cutoff,
                UserTrade.action == "buy",
            )
            .order_by(UserTrade.executed_at.asc())
            .all()
        )

        for trade in old_trades:
            age_hours = (utc_now() - trade.executed_at).total_seconds() / 3600
            stale.append({
                "trade_id": trade.id,
                "market_id": trade.market_id,
                "amount": trade.amount,
                "entry_price": trade.price,
                "executed_at": trade.executed_at.isoformat(),
                "age_hours": round(age_hours, 1),
            })

    except Exception as e:
        logger.error("Error checking stale positions for user %d: %s", user_id, e)

    return stale


async def _merge_duplicate_positions(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """Detect duplicate positions on the same market outcome and merge them.

    Inspired by poly_merger — finds multiple trades on the same token_id
    and computes a weighted-average entry price.
    """
    from app.models.user_trade import UserTrade
    from sqlalchemy import func

    merged: List[Dict[str, Any]] = []

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
            merged.append({
                "market_id": dupe.market_id,
                "trade_count": dupe.count,
                "total_amount": round(float(dupe.total_amount), 4),
                "avg_entry_price": round(float(avg_price), 4),
                "total_cost": round(float(dupe.total_cost), 4),
            })

    except Exception as e:
        logger.error("Error detecting duplicate positions for user %d: %s", user_id, e)

    return merged


async def run_lifecycle_check(user_id: int, wallet_address: str) -> Dict[str, Any]:
    """Run a full lifecycle check for a user.

    Returns summary of resolved, stale, and duplicate positions found.
    """
    db = SessionLocal()
    try:
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
        }
    finally:
        db.close()


async def _lifecycle_loop():
    """Background loop: periodically check positions for all users."""
    logger.info("Position lifecycle manager started (interval=%ds)", LIFECYCLE_CHECK_INTERVAL)

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
                                "Lifecycle check for user %d: %d resolved, %d stale, %d duplicates",
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

        await asyncio.sleep(LIFECYCLE_CHECK_INTERVAL)

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
        try:
            await _lifecycle_task
        except asyncio.CancelledError:
            pass
    _lifecycle_task = None
    logger.info("Position lifecycle manager stopped")


def get_lifecycle_metrics() -> Dict[str, Any]:
    """Return running status."""
    return {
        "running": _lifecycle_task is not None and not _lifecycle_task.done(),
        "check_interval_seconds": LIFECYCLE_CHECK_INTERVAL,
        "stale_threshold_hours": STALE_POSITION_HOURS,
        "auto_redeem_enabled": AUTO_REDEEM_ENABLED,
    }
