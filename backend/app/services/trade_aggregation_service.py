"""
Trade Aggregation Service — combine multiple small trades into larger orders.

When a followed trader makes several small buys/sells within a short time window,
this service aggregates them into a single larger order to reduce gas costs and
improve fill rates.

Inspired by PolymarketTrading/Polymarket-Trading's volume aggregation.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

from app.config import get_settings
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Default aggregation config
DEFAULT_WINDOW_SECONDS = 30  # Wait this long before executing aggregated trade
DEFAULT_MIN_TRADES = 2  # Minimum trades to trigger aggregation
DEFAULT_MAX_WINDOW_SECONDS = 120  # Maximum wait time even if trades keep flowing

# In-memory buffer of pending trades keyed by (user_id, token_id, side)
_pending_buffer: Dict[Tuple[int, str, str], List[Dict[str, Any]]] = defaultdict(list)
_buffer_timers: Dict[Tuple[int, str, str], datetime] = {}
_aggregation_task: Optional[asyncio.Task] = None


def add_trade_to_buffer(
    user_id: int,
    token_id: str,
    side: str,
    price: float,
    size: float,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Add a trade to the aggregation buffer.

    Returns True if the trade was buffered (caller should NOT execute immediately).
    Returns False if aggregation is disabled or not applicable.
    """
    settings = get_settings()
    window_seconds = getattr(settings, "trade_aggregation_window", DEFAULT_WINDOW_SECONDS)

    if window_seconds <= 0:
        return False  # Aggregation disabled

    key = (user_id, token_id, side.upper())

    entry = {
        "price": price,
        "size": size,
        "added_at": utc_now(),
        "metadata": metadata or {},
    }

    _pending_buffer[key].append(entry)

    # Record first trade time for this key
    if key not in _buffer_timers:
        _buffer_timers[key] = utc_now()

    logger.debug(
        "Buffered trade for aggregation: user=%d token=%s side=%s size=%.2f (buffer_count=%d)",
        user_id, token_id[:20], side, size, len(_pending_buffer[key]),
    )

    return True


def get_aggregated_trade(
    user_id: int,
    token_id: str,
    side: str,
) -> Optional[Dict[str, Any]]:
    """Check if we have a ready aggregated trade.

    Returns the aggregated trade info if the window has elapsed,
    otherwise None (keep waiting).
    """
    key = (user_id, token_id, side.upper())
    trades = _pending_buffer.get(key, [])
    first_time = _buffer_timers.get(key)

    if not trades or not first_time:
        return None

    settings = get_settings()
    window_seconds = getattr(settings, "trade_aggregation_window", DEFAULT_WINDOW_SECONDS)
    max_window = getattr(settings, "trade_aggregation_max_window", DEFAULT_MAX_WINDOW_SECONDS)

    now = utc_now()
    elapsed = (now - first_time).total_seconds()

    # Check if window has elapsed or max window exceeded
    if elapsed < window_seconds and elapsed < max_window:
        return None

    # Aggregate: VWAP price, total size
    total_size = sum(t["size"] for t in trades)
    total_cost = sum(t["size"] * t["price"] for t in trades)
    vwap_price = total_cost / total_size if total_size > 0 else 0

    result = {
        "user_id": user_id,
        "token_id": token_id,
        "side": side.upper(),
        "aggregated_size": round(total_size, 4),
        "vwap_price": round(vwap_price, 4),
        "trade_count": len(trades),
        "window_seconds": round(elapsed, 1),
        "individual_trades": [
            {
                "price": t["price"],
                "size": t["size"],
                "added_at": t["added_at"].isoformat(),
            }
            for t in trades
        ],
    }

    # Clear buffer
    _pending_buffer.pop(key, None)
    _buffer_timers.pop(key, None)

    return result


def flush_buffer(user_id: int, token_id: str, side: str) -> Optional[Dict[str, Any]]:
    """Force-flush the buffer for a specific key, regardless of window."""
    key = (user_id, token_id, side.upper())
    trades = _pending_buffer.get(key, [])

    if not trades:
        return None

    total_size = sum(t["size"] for t in trades)
    total_cost = sum(t["size"] * t["price"] for t in trades)
    vwap_price = total_cost / total_size if total_size > 0 else 0

    result = {
        "user_id": user_id,
        "token_id": token_id,
        "side": side.upper(),
        "aggregated_size": round(total_size, 4),
        "vwap_price": round(vwap_price, 4),
        "trade_count": len(trades),
        "flushed": True,
    }

    _pending_buffer.pop(key, None)
    _buffer_timers.pop(key, None)

    return result


def get_pending_buffers() -> Dict[str, Any]:
    """Get a summary of all pending aggregation buffers."""
    summary = {}
    for key, trades in _pending_buffer.items():
        user_id, token_id, side = key
        first_time = _buffer_timers.get(key)
        summary[f"{user_id}:{token_id[:16]}:{side}"] = {
            "count": len(trades),
            "total_size": round(sum(t["size"] for t in trades), 4),
            "waiting_since": first_time.isoformat() if first_time else None,
        }
    return summary


async def _aggregation_loop():
    """Background loop: check for ready aggregated trades and execute them."""
    logger.info("Trade aggregation service started")

    while True:
        try:
            # Check all pending buffers
            for key in list(_pending_buffer.keys()):
                user_id, token_id, side = key
                aggregated = get_aggregated_trade(user_id, token_id, side)
                if aggregated:
                    logger.info(
                        "Aggregated trade ready: user=%d token=%s side=%s "
                        "size=%.2f vwap=%.4f (from %d trades over %.1fs)",
                        user_id,
                        token_id[:20],
                        side,
                        aggregated["aggregated_size"],
                        aggregated["vwap_price"],
                        aggregated["trade_count"],
                        aggregated["window_seconds"],
                    )

                    # Execute the aggregated trade via copy_trade_service
                    try:
                        from app.services.copy_trade_service import execute_aggregated_trade
                        await execute_aggregated_trade(aggregated)
                    except ImportError:
                        logger.debug(
                            "execute_aggregated_trade not available yet, "
                            "logging aggregated trade only"
                        )
                    except Exception as e:
                        logger.error("Failed to execute aggregated trade: %s", e)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Aggregation loop error: %s", e)

        await asyncio.sleep(5)  # Check every 5 seconds

    # Flush all pending on shutdown
    for key in list(_pending_buffer.keys()):
        user_id, token_id, side = key
        flushed = flush_buffer(user_id, token_id, side)
        if flushed:
            logger.info("Flushed pending aggregation on shutdown: %s", flushed)

    logger.info("Trade aggregation service stopped")


async def start_aggregation_service():
    """Start the trade aggregation background service."""
    global _aggregation_task
    if _aggregation_task and not _aggregation_task.done():
        return
    _aggregation_task = asyncio.create_task(_aggregation_loop())


async def stop_aggregation_service():
    """Stop the trade aggregation background service."""
    global _aggregation_task
    if _aggregation_task and not _aggregation_task.done():
        _aggregation_task.cancel()
        try:
            await _aggregation_task
        except asyncio.CancelledError:
            pass
    _aggregation_task = None
