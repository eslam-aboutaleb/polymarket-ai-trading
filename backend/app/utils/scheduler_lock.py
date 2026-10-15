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

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.utils.cache import get_cache
from app.utils.database import engine

logger = logging.getLogger(__name__)

# name -> dedicated DBAPI connection holding the session-level lock
_held_locks: dict[str, Any] = {}

# Count of lock acquisitions that failed open (database error). A
# non-zero value means a monitor is running on the process-local guard
# only — with more than one worker that is exactly the duplicate-order
# hazard the advisory locks exist to prevent, so the watchdog surfaces
# it (plan 10 Phase A).
_lock_fail_open_total = 0

_metrics = {"fail_open_total": 0}


def _lock_engine():
    """A connection factory for advisory-lock sessions.

    Advisory locks are *session-level* in Postgres, so a lock lives as
    long as the connection that took it. The application's pooled engine
    hands the same session back after ``close()``, which meant an
    acquire/release pair could leave the lock held on a session the pool
    still owns — and the next acquire of that name then failed as "held
    by another worker" even though this process was the only caller.
    ``NullPool`` closes the DBAPI connection for real, so release means
    release.

    Built lazily and cached: importing this module must not open a
    connection (a web-only process that never runs a scheduler would
    otherwise pay for one on import).
    """
    global _lock_engine_cached
    if _lock_engine_cached is None:
        _lock_engine_cached = create_engine(str(engine.url), poolclass=NullPool)
    return _lock_engine_cached


_lock_engine_cached = None


def _metric_inc(key: str, by: int = 1) -> None:
    _metrics[key] = _metrics.get(key, 0) + by


def get_scheduler_lock_metrics() -> dict[str, int]:
    """Lock metrics for the watchdog and /api/health.

    ``fail_open_total`` is the number of schedulers now running with
    no cross-process guard. It must stay 0 in any multi-worker
    deployment.
    """
    return dict(_metrics)


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
    "forecast_resolution": 21600,
    "model_drift": 3600,
    "daily_digest": 86400,
    "equity_snapshots": 3600,
    "venue_markets": 300,
}
DEFAULT_INTERVAL_SECONDS = 300


def acquire_scheduler_lock(name: str) -> bool:
    """Acquire the session-level advisory lock for a named scheduler.

    Returns True when this process now owns (or already held) the lock.
    Returns False when another worker holds it — the caller must skip
    starting its loop. On a database error the lock fails open with a
    WARNING and an ERROR log plus a metric increment: the shipped
    deployment runs a single worker, so a transient database outage
    must not permanently disable a monitor, but a silent fail-open in a
    multi-worker deployment is how duplicate stop-loss orders happen.
    """
    if name in _held_locks:
        return True

    try:
        connection = _lock_engine().connect()
    except Exception:
        _fail_open(name)
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
        _fail_open(name)
        return True

    if acquired:
        _held_locks[name] = connection
        logger.info("Acquired scheduler lock '%s'", name)
        return True

    connection.close()
    logger.warning("Scheduler lock '%s' is held by another worker; skipping start", name)
    return False


def _fail_open(name: str) -> None:
    """Record a lock that could not be acquired because of a DB error."""
    global _lock_fail_open_total
    _lock_fail_open_total += 1
    _metric_inc("fail_open_total")
    logger.error(
        "Could not acquire scheduler lock '%s' (database error); proceeding with the "
        "process-local guard ONLY. With more than one worker running this monitor "
        "may now duplicate orders — fix the database before scaling workers.",
        name,
    )


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
