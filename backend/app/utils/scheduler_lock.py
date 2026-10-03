"""Named Postgres advisory locks and dead-man's-switch heartbeats.

Every background scheduler acquires a session-level advisory lock keyed
by its name before starting its loop. A second worker process (or a
duplicate scheduler) fails to acquire the lock and skips with a WARNING,
so exactly one instance of each monitor runs per database. Locks are
held on a dedicated connection until release or process exit — Postgres
releases them automatically when the connection closes, so a crashed
worker can never hold a lock forever.

Heartbeats implement the dead-man's switch: each loop iteration writes
``heartbeat:<name>`` (24h TTL) and the watchdog in ``app.main`` alerts
when a heartbeat goes stale. The TTL is deliberately long so a dead
scheduler's last heartbeat stays readable — and keeps alerting — until
an operator intervenes.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.utils.cache import get_cache
from app.utils.database import engine

logger = logging.getLogger(__name__)

# name -> dedicated DBAPI connection holding the session-level lock
_held_locks: dict[str, Any] = {}

_heartbeat_cache = get_cache("scheduler")

# Heartbeat TTL: long enough that a dead scheduler's last heartbeat
# stays readable (and keeps alerting) until an operator intervenes.
HEARTBEAT_TTL_SECONDS = 86400

# Check interval (seconds) per named scheduler. Used by the watchdog to
# compute per-scheduler staleness thresholds. Schedulers not listed here
# fall back to DEFAULT_INTERVAL_SECONDS.
SCHEDULER_INTERVALS: dict[str, int] = {
    "stop_loss_monitor": 10,
    "inverse_bot_monitor": 300,
    "market_maker": 30,
    "position_lifecycle": 300,
    "arbitrage_monitor": 120,
    "trade_monitor": 60,
    "leaderboard_refresh": 300,
    "trade_aggregation": 300,
    "news_generator": 3600,
    "redemption": 300,
    "ctf_events": 300,
    "latency_arb": 5,
}
DEFAULT_INTERVAL_SECONDS = 300


def acquire_scheduler_lock(name: str) -> bool:
    """Acquire the session-level advisory lock for a named scheduler.

    Returns True when this process now owns (or already held) the lock.
    Returns False when another worker holds it — the caller must skip
    starting its loop. On a database error the lock fails open with a
    WARNING: the shipped deployment runs a single worker, so a
    transient database outage must not permanently disable a monitor.
    """
    if name in _held_locks:
        return True

    try:
        connection = engine.connect()
    except Exception:
        logger.warning(
            "Could not acquire scheduler lock '%s' (database error); "
            "proceeding with process-local guard only",
            name,
        )
        return True

    try:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"),
                {"name": name},
            ).scalar()
        )
    except Exception:
        connection.close()
        logger.warning(
            "Could not acquire scheduler lock '%s' (database error); "
            "proceeding with process-local guard only",
            name,
        )
        return True

    if acquired:
        _held_locks[name] = connection
        logger.info("Acquired scheduler lock '%s'", name)
        return True

    connection.close()
    logger.warning("Scheduler lock '%s' is held by another worker; skipping start", name)
    return False


def release_scheduler_lock(name: str) -> None:
    """Release a previously acquired advisory lock."""
    connection = _held_locks.pop(name, None)
    if connection is None:
        return
    try:
        connection.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": name})
        logger.info("Released scheduler lock '%s'", name)
    except Exception:
        logger.warning("Failed to release scheduler lock '%s'", name)
    finally:
        connection.close()


def scheduler_heartbeat(name: str) -> None:
    """Refresh the dead-man's-switch heartbeat for a named scheduler."""
    try:
        _heartbeat_cache.set(
            f"heartbeat:{name}",
            datetime.now(UTC).isoformat(),
            HEARTBEAT_TTL_SECONDS,
        )
    except Exception:
        logger.debug("Heartbeat write failed for '%s'", name, exc_info=True)


def scheduler_heartbeat_age(name: str) -> float | None:
    """Seconds since the scheduler's last heartbeat, None when never seen."""
    try:
        raw = _heartbeat_cache.get(f"heartbeat:{name}")
        if not raw:
            return None
        last_seen = datetime.fromisoformat(str(raw))
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last_seen).total_seconds()
    except Exception:
        return None


def scheduler_stale_threshold(name: str) -> int:
    """Staleness threshold (seconds) for a scheduler's heartbeat."""
    interval = SCHEDULER_INTERVALS.get(name, DEFAULT_INTERVAL_SECONDS)
    return max(3 * interval, 120)


def is_scheduler_stale(name: str) -> bool | None:
    """True when the heartbeat is stale; None when never seen."""
    age = scheduler_heartbeat_age(name)
    if age is None:
        return None
    return age > scheduler_stale_threshold(name)
