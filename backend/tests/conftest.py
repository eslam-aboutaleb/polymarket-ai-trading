"""Shared pytest configuration for the backend test suite.

Settings validation runs at import time in several modules, so the required
secrets are seeded once here. ``setdefault`` is used deliberately: a test that
needs a specific value still overrides it via ``patch.dict(os.environ, ...)``,
and tests such as ``test_security_config.py`` that assert on invalid values
pass them explicitly to ``Settings(...)``.

Keeping this in one place stops an individual test module from failing to import
merely because it touches configuration first.
"""

import contextlib
import os
from typing import Any

# Required by Settings.validate_jwt_secret_key: at least 32 characters and not
# a recognised placeholder. Any 32-character string satisfies the check.
os.environ.setdefault("JWT_SECRET_KEY", "a" * 32)
os.environ.setdefault("REFRESH_TOKEN_HASH_SECRET", "b" * 32)

# Settings refuses to start against production defaults implicitly; the test
# environment is explicitly non-production.
os.environ.setdefault("ENVIRONMENT", "test")

# The suite must observe the schedulers that the app normally starts, or the
# lifespan tests silently assert against a process that owns none. Pinned here
# rather than left to the developer's .env, where a stray ROLE=web (the value
# docker-compose gives the web tier) makes those tests fail for a reason that
# has nothing to do with the change under test.
os.environ.setdefault("ROLE", "all")

import pytest  # noqa: E402  (must follow the environment seeding above)

# Baseline ``app.main.settings`` object, captured the first time the autouse
# fixture sweeps. ``importlib.reload(app.main)`` re-executes the module against
# whatever environment is current at reload time, so a test that reloads under
# a patched ``ENVIRONMENT`` would otherwise leave a production-flavoured
# settings object behind for every later test — e.g. the general exception
# handler masking error detail when ``settings.is_local_environment`` is False.
_main_settings_baseline: Any = None


def _cancel_task(task: Any) -> None:
    """Best-effort cancel of a module-level asyncio task handle.

    The handle may be ``None``, a test double, or a task owned by an event
    loop an earlier test already closed, so every cancellation failure is
    swallowed rather than aborting the sweep.
    """
    with contextlib.suppress(Exception):
        task.cancel()


def _reset_news_service() -> None:
    """Clear the background-generator task handle and the service singleton."""
    import app.services.news_service as news_service

    _cancel_task(news_service._generator_task)
    news_service._generator_task = None
    news_service._news_service = None


def _reset_trade_aggregation_service() -> None:
    """Clear the aggregation-loop task handle and the in-memory buffers."""
    from collections import defaultdict

    import app.services.trade_aggregation_service as aggregation_service

    _cancel_task(aggregation_service._aggregation_task)
    aggregation_service._aggregation_task = None
    aggregation_service._pending_buffer = defaultdict(list)
    aggregation_service._buffer_timers = {}


def _reset_trade_monitor() -> None:
    """Clear the monitor/reconciliation tasks and all in-memory monitor state."""
    import asyncio

    import app.services.trade_monitor as trade_monitor

    for attribute in ("_monitor_task", "_fill_recon_task", "_gtc_recon_task"):
        _cancel_task(getattr(trade_monitor, attribute, None))
        setattr(trade_monitor, attribute, None)
    trade_monitor._watched_wallets = set()
    trade_monitor._last_seen_trades = {}
    trade_monitor._processed_trade_ids = {}
    trade_monitor._poll_http_client = None
    trade_monitor._poll_semaphore = asyncio.Semaphore(trade_monitor._POLL_MAX_CONCURRENCY)
    trade_monitor._processed_ids_cache = None


def _isolate_advisory_locks() -> None:
    """Point the scheduler-lock engine at SQLite so locks fail open.

    Advisory locks are *session-level* Postgres locks, so one held by a
    developer's running backend on the shared dev database is
    indistinguishable from "another worker owns this scheduler" to a
    test process. Almost every test asserts on a loop's behaviour, not
    on lock contention, so they need the fail-open path the suite was
    originally written against (it only worked because the tests then
    ran on SQLite, where ``pg_try_advisory_lock`` does not exist).

    ``tests/test_scheduler_lock.py`` patches ``_lock_engine`` itself and
    is therefore unaffected.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from app.utils import scheduler_lock

    scheduler_lock._lock_engine_cached = create_engine("sqlite://", poolclass=NullPool)


def _release_scheduler_locks() -> None:
    """Release advisory locks a test left tracked in ``_held_locks``.

    ``_held_locks`` maps a lock name to the dedicated connection holding a
    session-level Postgres advisory lock. A loop that exits without
    ``release_scheduler_lock`` (for instance a cancellation raised outside the
    loop's try block) leaves the entry behind; releasing here — before any
    other test's ``_held_locks.clear()`` can orphan the connection back to the
    pool still holding the database-side lock — keeps later acquisitions honest.
    """
    from app.utils import scheduler_lock

    for name in list(scheduler_lock._held_locks):
        with contextlib.suppress(Exception):
            scheduler_lock.release_scheduler_lock(name)
    scheduler_lock._held_locks.clear()
    scheduler_lock._lock_fail_open_total = 0
    scheduler_lock._metrics["fail_open_total"] = 0


def _reset_settings_state() -> None:
    """Restore the import-time settings baseline and drop the cached one."""
    global _main_settings_baseline

    import app.main as main_module
    from app.config import get_settings

    if _main_settings_baseline is None:
        _main_settings_baseline = main_module.settings
    else:
        main_module.settings = _main_settings_baseline
    get_settings.cache_clear()


def _reset_observability_metrics() -> None:
    """Zero the observability counters shared by the venue/daily-report services."""
    from app.services.daily_report_service import reset_daily_report_metrics
    from app.services.venue_market_service import reset_venue_market_metrics

    reset_venue_market_metrics()
    reset_daily_report_metrics()


_RESETTERS = (
    _reset_news_service,
    _reset_trade_aggregation_service,
    _reset_trade_monitor,
    _isolate_advisory_locks,
    _release_scheduler_locks,
    _reset_settings_state,
    _reset_observability_metrics,
)


def _sweep_module_singletons() -> None:
    """Run every resetter, isolating each one's import side effects."""
    for resetter in _RESETTERS:
        with contextlib.suppress(Exception):
            resetter()


@pytest.fixture(autouse=True)
def _reset_module_singletons():
    """Reset module-level singletons/caches between tests.

    Several services keep background-task handles, in-memory buffers,
    advisory locks, settings objects, or observability counters at module
    scope ("refresh once" flags, adapter health, digest metrics). Left
    alone, the second test to touch such a service sees the first
    test's state, so an assertion like "this run refreshed exactly
    once" — or "starting the monitor created a fresh HTTP client" —
    only holds in whichever order the files happen to load.
    Randomizing test order makes that failure deterministic instead of
    incidental, so each test now starts from a known state.

    The sweep runs both before and after every test: the pre-sweep
    restores the baseline, and the post-sweep additionally cancels the
    tasks a test created and releases the advisory locks it acquired so
    neither survives into the next test.
    """
    _sweep_module_singletons()

    yield

    _sweep_module_singletons()
