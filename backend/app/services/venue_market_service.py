"""Cached cross-venue market rows and the venue refresh loop (plan 05 Phase A).

**Read-only and signal-only.** This service fetches public market data,
normalizes it and stores the latest snapshot per ``(venue, market_id)``.
It never places, cancels or sizes an order: the rows are comparison
signal for a human, and any trade stays manual confirmation (plan 05
key decision 4).

Off by default: every venue has its own feature flag (``venue_<name>_
enabled``) so an operator opts in per venue, which is also the ToS
mitigation — an adapter only ever calls its venue's documented public
endpoint, once per refresh, behind a TTL cache.

Loop shape follows the repo's scheduler idiom (``equity_snapshot_
service._snapshot_loop``): acquire the ``venue_markets`` advisory lock,
run one pass, write the dead-man's-switch heartbeat, sleep, release on
exit. Writes are idempotent — the unique ``(venue, market_id)`` plus an
update-in-place means a re-run refreshes rows instead of duplicating
them, which matters because the advisory lock fails open on a database
error. A venue that disappears from a *non-empty* payload is pruned
(its market closed); an empty payload prunes nothing, so one bad pass
can never wipe the cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.venue_market import VenueMarket
from app.schemas.market import NormalizedMarket
from app.services.venues.base import registry
from app.services.venues.kalshi import KalshiAdapter
from app.services.venues.manifold import ManifoldAdapter
from app.services.venues.predictit import PredictItAdapter
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

SCHEDULER_NAME = "venue_markets"

# Plan 05 Phase A: refresh registered adapters every 5 minutes.
DEFAULT_REFRESH_SECONDS = 300
# One upstream call per adapter per pass (Kalshi caps a page at 200).
FETCH_LIMIT = 200
DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 500

# Bootstrap: Kalshi is the reference adapter (public REST API, no
# certs, US-regulated, real money); Manifold is play money and
# PredictIt is real money but the most legally constrained of the
# three. Registration is idempotent, so re-importing this module (or a
# test registering it again) is a no-op. Each stays dark until its own
# ``venue_<name>_enabled`` flag is set.
registry.register("kalshi", KalshiAdapter())
registry.register("manifold", ManifoldAdapter())
registry.register("predictit", PredictItAdapter())

_refresh_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_running = False

# venue -> {"last_fetch_at": iso, "last_error": str, "markets": int}
_adapter_states: dict[str, dict[str, Any]] = {}
_metrics = {
    "refreshes": 0,
    "rows_written": 0,
    "errors": 0,
    "last_refresh_at": None,
}

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _metric_inc(key: str, by: int = 1) -> None:
    _metrics[key] = _metrics.get(key, 0) + by


def refresh_interval() -> int:
    """Effective loop interval in seconds (floored at 60s)."""
    try:
        return max(60, int(get_settings().venue_markets_refresh_seconds))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_REFRESH_SECONDS


def is_venue_enabled(name: str) -> bool:
    """Per-venue feature flag (``venue_<name>_enabled``), off by default."""
    try:
        return bool(getattr(get_settings(), f"venue_{name}_enabled", False))
    except Exception:
        return False


def _adapter_state(name: str) -> dict[str, Any]:
    return _adapter_states.setdefault(
        name,
        {"last_fetch_at": None, "last_error": None, "markets": 0},
    )


def _record_success(name: str, markets: int) -> None:
    state = _adapter_state(name)
    state["last_fetch_at"] = utc_now().isoformat()
    state["last_error"] = None
    state["markets"] = markets


def _record_error(name: str, exc: Exception) -> None:
    state = _adapter_state(name)
    state["last_error"] = f"{type(exc).__name__}: {exc}"
    state["last_fetch_at"] = utc_now().isoformat()
    _metric_inc("errors")


def _record_skipped(name: str) -> None:
    state = _adapter_state(name)
    state.setdefault("last_fetch_at", None)
    state.setdefault("last_error", None)
    state.setdefault("markets", 0)


# ── writer ───────────────────────────────────────────────────────


def _prices_blob(market: NormalizedMarket) -> str:
    payload = {
        "yes": market.yes_price,
        "no": market.no_price,
        "outcomes": list(market.outcomes),
        "url": market.url,
        "is_real_money": market.is_real_money,
        "raw_hash": market.raw_hash,
    }
    return json.dumps(payload)


def _write_markets(db: Session, markets: list[NormalizedMarket]) -> int:
    """Upsert one venue's snapshot rows and prune markets it dropped.

    Returns the number of rows written. An empty list writes nothing and
    prunes nothing: a venue that returned no markets this pass (or an
    adapter that degraded to ``[]``) must not wipe the cache.
    """
    if not markets:
        return 0
    now = utc_now()
    venue = markets[0].venue
    live_ids: set[str] = set()
    for market in markets:
        row = (
            db.query(VenueMarket)
            .filter(
                VenueMarket.venue == market.venue,
                VenueMarket.market_id == market.market_id,
            )
            .first()
        )
        if row is None:
            row = VenueMarket(venue=market.venue, market_id=market.market_id)
            db.add(row)
        row.question = market.question
        row.prices_json = _prices_blob(market)
        row.volume_24h = market.volume_24h
        row.liquidity = market.liquidity
        row.end_date = market.end_date
        row.fetched_at = now
        live_ids.add(market.market_id)
    db.query(VenueMarket).filter(
        VenueMarket.venue == venue,
        VenueMarket.market_id.notin_(live_ids),
    ).delete(synchronize_session=False)
    db.commit()
    return len(markets)


async def refresh_venue_markets(db: Session | None = None) -> dict[str, int]:
    """One refresh pass over every enabled, registered adapter.

    Per-venue failures are isolated: a venue that errors keeps its
    previous rows and is recorded in ``_adapter_states``. ``db`` may be
    supplied by a caller that already holds a session; otherwise the
    pass opens (and closes) its own.
    """
    summary = {"venues": 0, "rows": 0, "errors": 0, "skipped": 0}
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        for adapter in registry.all():
            name = adapter.name
            if not is_venue_enabled(name):
                _record_skipped(name)
                summary["skipped"] += 1
                continue
            try:
                raw = await adapter.fetch_markets(limit=FETCH_LIMIT)
                markets = adapter.parse_markets(raw)
                written = _write_markets(db, markets)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.rollback()
                _record_error(name, exc)
                summary["errors"] += 1
                continue
            _record_success(name, written)
            summary["venues"] += 1
            summary["rows"] += written
    finally:
        if owns_session:
            db.close()

    _metric_inc("refreshes")
    _metrics["rows_written"] += summary["rows"]
    _metrics["last_refresh_at"] = utc_now().isoformat()
    return summary


# ── read path ────────────────────────────────────────────────────


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a query string matches literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def serialize_venue_market(row: VenueMarket) -> dict[str, Any]:
    """Wire shape of one cached row (prices live in ``prices_json``)."""
    try:
        prices = json.loads(row.prices_json) if row.prices_json else {}
    except (TypeError, ValueError):
        prices = {}
    return {
        "venue": row.venue,
        "market_id": row.market_id,
        "question": row.question,
        "yes_price": prices.get("yes"),
        "no_price": prices.get("no"),
        "outcomes": prices.get("outcomes") or ["Yes", "No"],
        "volume_24h": row.volume_24h,
        "liquidity": row.liquidity,
        "end_date": row.end_date.isoformat() if row.end_date else None,
        "url": prices.get("url", ""),
        "is_real_money": bool(prices.get("is_real_money", True)),
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
    }


def get_venue_markets(
    db: Session,
    *,
    venue: str | None = None,
    q: str | None = None,
    limit: int = DEFAULT_QUERY_LIMIT,
) -> list[VenueMarket]:
    """Latest cached rows, most liquid first.

    The limit is clamped so a hostile query string cannot pull the whole
    table, and the search term's LIKE wildcards are escaped so it
    matches literally.
    """
    clamped = max(1, min(int(limit or DEFAULT_QUERY_LIMIT), MAX_QUERY_LIMIT))
    query = db.query(VenueMarket)
    if venue:
        query = query.filter(VenueMarket.venue == venue)
    if q:
        query = query.filter(VenueMarket.question.ilike(f"%{_escape_like(q)}%", escape="\\"))
    return (
        query.order_by(VenueMarket.liquidity.desc(), VenueMarket.market_id.asc())
        .limit(clamped)
        .all()
    )


def normalize_question(question: str) -> str:
    """Order-independent key for exact-match grouping across venues.

    Phase A only: lowercase, punctuation stripped, tokens sorted. Phase B
    adds the fuzzy-token matcher on top of this (plan 05 key decision 4).
    """
    tokens = {token for token in _TOKEN_SPLIT.split((question or "").lower()) if token}
    return " ".join(sorted(tokens))


def get_cross_venue_groups(
    db: Session,
    query: str,
    *,
    limit: int = DEFAULT_QUERY_LIMIT,
    min_venues: int = 2,
) -> list[dict[str, Any]]:
    """Rows grouped by normalized question, keeping only multi-venue keys.

    Both venues' questions travel with the group so a human can confirm
    the match before acting on it — a stale or wrong match is the main
    fake-arb risk (plan 05 Risks).
    """
    rows = get_venue_markets(db, q=query, limit=MAX_QUERY_LIMIT)
    grouped: dict[str, list[VenueMarket]] = {}
    for row in rows:
        key = normalize_question(row.question)
        if key:
            grouped.setdefault(key, []).append(row)

    groups: list[dict[str, Any]] = []
    for key, group_rows in grouped.items():
        venues = {row.venue for row in group_rows}
        if len(venues) < min_venues:
            continue
        serialized = [serialize_venue_market(row) for row in group_rows]
        yes_prices = [row["yes_price"] for row in serialized if row["yes_price"] is not None]
        groups.append(
            {
                "key": key,
                "venues": sorted(venues),
                "questions": {row["venue"]: row["question"] for row in serialized},
                "rows": serialized,
                "yes_min": min(yes_prices) if yes_prices else None,
                "yes_max": max(yes_prices) if yes_prices else None,
                "edge": round(max(yes_prices) - min(yes_prices), 4) if yes_prices else None,
                "max_liquidity": max((row["liquidity"] for row in serialized), default=0.0),
            }
        )
    groups.sort(key=lambda group: (-group["max_liquidity"], group["key"]))
    return groups[: max(1, min(int(limit or DEFAULT_QUERY_LIMIT), MAX_QUERY_LIMIT))]


def get_adapter_health() -> list[dict[str, Any]]:
    """Health and last-fetch state per registered adapter."""
    health: list[dict[str, Any]] = []
    for name in registry.names():
        state = _adapter_states.get(name, {})
        health.append(
            {
                "name": name,
                "enabled": is_venue_enabled(name),
                "last_fetch_at": state.get("last_fetch_at"),
                "last_error": state.get("last_error"),
                "markets": state.get("markets", 0),
            }
        )
    return health


def get_venue_market_metrics() -> dict[str, Any]:
    """Counters for the watchdog and the venues page."""
    return {
        "running": is_venue_market_running(),
        "interval_seconds": refresh_interval(),
        "scheduler_name": SCHEDULER_NAME,
        "refreshes": _metrics["refreshes"],
        "rows_written": _metrics["rows_written"],
        "errors": _metrics["errors"],
        "last_refresh_at": _metrics["last_refresh_at"],
        "adapters": get_adapter_health(),
    }


def reset_venue_market_metrics() -> None:
    """Zero the counters and the per-adapter health state.

    Both are module-level singletons, so without this a test that runs
    after one which touched the service observes that run's counters
    rather than its own — an order-dependent failure that only surfaces
    once the suite is randomized.
    """
    for key in ("refreshes", "rows_written", "errors"):
        _metrics[key] = 0
    _metrics["last_refresh_at"] = None
    _adapter_states.clear()


# ── lifecycle ────────────────────────────────────────────────────


async def _venue_market_loop() -> None:
    """Advisory-locked refresh loop: work → heartbeat → sleep."""
    if not acquire_scheduler_lock(SCHEDULER_NAME):
        return
    interval = refresh_interval()
    logger.info("Venue market refresh started (interval=%ds)", interval)
    try:
        while _stop_event is not None and not _stop_event.is_set():
            try:
                db = SessionLocal()
                try:
                    await refresh_venue_markets(db)
                finally:
                    db.close()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Venue market refresh pass failed: %s", exc)
            scheduler_heartbeat(SCHEDULER_NAME)
            if _stop_event is None:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_stop_event.wait(), timeout=interval)
    finally:
        release_scheduler_lock(SCHEDULER_NAME)
    logger.info("Venue market refresh stopped")


def is_venue_market_running() -> bool:
    """Whether the refresh task is alive (metrics/health checks)."""
    return _refresh_task is not None and not _refresh_task.done()


async def start_venue_market_service() -> None:
    """Start the venue refresh loop (skips when the lock is held)."""
    global _refresh_task, _stop_event, _running
    if _running:
        return
    _stop_event = asyncio.Event()
    _running = True
    _refresh_task = asyncio.create_task(_venue_market_loop())
    logger.info("Venue market refresh task started")


async def stop_venue_market_service() -> None:
    """Stop the venue refresh loop."""
    global _refresh_task, _stop_event, _running
    if not _running:
        return
    _running = False
    if _stop_event is not None:
        _stop_event.set()
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Venue market refresh stop error: %s", exc)
    _refresh_task = None
    _stop_event = None
    logger.info("Venue market refresh task stopped")
