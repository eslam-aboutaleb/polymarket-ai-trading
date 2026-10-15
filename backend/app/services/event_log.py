"""Event log writer/reader for ``AppEvent`` (plan 10 Phase A).

Design contract
---------------
**Append-only from the application side.** No code path updates or
deletes an ``AppEvent``; a test asserts the ORM never routes those
verbs at the model (see ``test_event_log.py``).

**Never blocks the money path.** ``append`` adds and flushes on the
*session the caller already owns*, so the caller's commit decides
durability — an event is only durable when the action it describes
committed. ``append_non_blocking`` schedules the write on a bounded
background queue for call sites that hold no session (a service loop,
say), and drops with a loud log rather than blocking when the queue is
full. A lost event is a degraded audit trail, not a failed trade.

**No PII in the payload.** Events carry ids and amounts. Never a
private key, never a full wallet address — callers pass what they need
and the payload cap below enforces a bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.app_event import AppEvent
from app.utils.database import SessionLocal
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Payload cap: Postgres TEXT is unbounded, but an unbounded audit row
# is a storage incident waiting to happen. Oversized payloads are
# truncated with a marker rather than rejected.
MAX_PAYLOAD_CHARS = 4000

# Bounded queue for the non-blocking path. Sized so a burst of order
# submissions never exhausts memory; drops are counted and exposed.
_QUEUE_MAXSIZE = 2000
_queue: asyncio.Queue[AppEvent] | None = None
_worker_task: asyncio.Task | None = None

_metrics = {
    "appended": 0,
    "queue_appended": 0,
    "dropped": 0,
    "failed": 0,
}


def _metric_inc(key: str, by: int = 1) -> None:
    _metrics[key] = _metrics.get(key, 0) + by


def get_event_log_metrics() -> dict[str, int]:
    """Counters for the event log (observability only)."""
    return dict(_metrics)


# ── canonical event vocabulary ───────────────────────────────
# Aggregate / event_type pairs emitted by the money path. Adding one
# here is what makes the log queryable by an audit reader.
TRADE_EVENTS = (
    "order_submitted",
    "order_fill_observed",
    "order_failed",
    "order_rejected_by_gate",
)

# UserTrade.status -> AppEvent.event_type. A submitted order is
# "order_submitted" until the trade monitor observes its fill, which
# then emits "order_fill_observed"; simulated and paper fills are
# logged as submissions so the volume is visible without pretending a
# fill happened.
TRADE_EVENT_BY_STATUS = {
    "pending": "order_submitted",
    "submitted": "order_submitted",
    "failed": "order_failed",
    "rejected": "order_rejected_by_gate",
    "executed": "order_fill_observed",
    "simulated": "order_submitted",
}

ACTOR_EVENTS = (
    "credential_stored",
    "credential_deleted",
    "strategy_enabled",
    "strategy_disabled",
    "account_created",
    "account_switched",
    "settings_changed",
)

KNOWN_AGGREGATES = ("trade", "order", "settings", "credential", "account", "strategy")


def _safe_payload(payload: dict[str, Any] | None) -> str | None:
    """Serialise a payload, truncating rather than failing."""
    if not payload:
        return None
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return json.dumps({"_unserializable": True})
    if len(text) > MAX_PAYLOAD_CHARS:
        return json.dumps({"_truncated": text[: MAX_PAYLOAD_CHARS - 40]})
    return text


def append(
    db: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | int | None = None,
    user_id: int | None = None,
    account_id: int | None = None,
    payload: dict[str, Any] | None = None,
    source: str | None = None,
) -> AppEvent | None:
    """Append one event on the caller's session; returns the row or None.

    The caller's commit decides durability, so a rolled-back action
    leaves no event. Callers must therefore append *inside* the
    transaction that performs the action — never after it.
    """
    try:
        event = AppEvent(
            user_id=user_id,
            account_id=account_id,
            aggregate_type=aggregate_type,
            aggregate_id=None if aggregate_id is None else str(aggregate_id)[:200],
            event_type=event_type,
            payload_json=_safe_payload(payload),
            source=source,
        )
        db.add(event)
        db.flush()
        _metric_inc("appended")
        return event
    except Exception as exc:
        _metric_inc("failed")
        logger.error(
            "Failed to append app event %s/%s for user=%s: %s",
            aggregate_type,
            event_type,
            user_id,
            exc,
        )
        return None


def append_non_blocking(
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | int | None = None,
    user_id: int | None = None,
    account_id: int | None = None,
    payload: dict[str, Any] | None = None,
    source: str | None = None,
) -> None:
    """Queue one event for a background writer; never blocks, never raises.

    For call sites without a live session (service loops, monitors).
    The event is written on a fresh session, so it is best-effort: a
    process killed before the writer drains the queue loses those
    events, which is the documented trade for not blocking the money
    path.
    """
    global _queue
    if _queue is None:
        return
    event = AppEvent(
        user_id=user_id,
        account_id=account_id,
        aggregate_type=aggregate_type,
        aggregate_id=None if aggregate_id is None else str(aggregate_id)[:200],
        event_type=event_type,
        payload_json=_safe_payload(payload),
        source=source,
    )
    try:
        _queue.put_nowait(event)
        _metric_inc("queue_appended")
    except asyncio.QueueFull:
        _metric_inc("dropped")
        logger.error(
            "Event log queue full — dropped %s/%s for user=%s",
            aggregate_type,
            event_type,
            user_id,
        )


async def _writer_loop() -> None:
    """Drain the queue one event at a time on its own session."""
    global _queue
    if _queue is None:  # pragma: no cover - defensive
        return
    while True:
        event = await _queue.get()
        db = SessionLocal()
        try:
            db.add(event)
            db.commit()
        except Exception as exc:
            _metric_inc("failed")
            logger.error("Event log writer failed for %s: %s", event.event_type, exc)
            with contextlib.suppress(Exception):
                db.rollback()
        finally:
            db.close()


def start_event_writer() -> None:
    """Start the non-blocking writer (idempotent; called by main's lifespan).

    Skipped on roles that own no schedulers: a web-only process should
    not hold a DB writer loop either.
    """
    global _queue, _worker_task
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_writer_loop())
    logger.info("Event log writer started")


async def stop_event_writer() -> None:
    """Stop the non-blocking writer, draining what is already queued."""
    global _worker_task, _queue
    if _worker_task is not None:
        _worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _worker_task
        _worker_task = None
    if _queue is not None:
        while not _queue.empty():
            event = _queue.get_nowait()
            db = SessionLocal()
            try:
                db.add(event)
                db.commit()
            except Exception:  # pragma: no cover - best effort drain
                with contextlib.suppress(Exception):
                    db.rollback()
            finally:
                db.close()
        _queue = None
    logger.info("Event log writer stopped")


def get_events(
    db: Session,
    *,
    user_id: int | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | int | None = None,
    event_types: list[str] | None = None,
    since: Any = None,
    until: Any = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read events newest-first, with the same filter set an audit reader needs.

    ``since``/``until`` accept a datetime or a ``timedelta`` (relative to
    now). Returns plain dicts so the response never leaks an ORM
    instance.
    """
    now = utc_now()
    if isinstance(since, timedelta):
        since = now - since
    if isinstance(until, timedelta):
        until = now - until

    query = db.query(AppEvent)
    if user_id is not None:
        query = query.filter(AppEvent.user_id == user_id)
    if aggregate_type:
        query = query.filter(AppEvent.aggregate_type == aggregate_type)
    if aggregate_id is not None:
        query = query.filter(AppEvent.aggregate_id == str(aggregate_id))
    if event_types:
        query = query.filter(AppEvent.event_type.in_(event_types))
    if since is not None:
        query = query.filter(AppEvent.occurred_at >= since)
    if until is not None:
        query = query.filter(AppEvent.occurred_at <= until)

    rows = query.order_by(AppEvent.occurred_at.desc(), AppEvent.id.desc()).limit(
        max(1, min(int(limit), 1000))
    )
    return [_event_to_dict(row) for row in rows.all()]


def _event_to_dict(row: AppEvent) -> dict[str, Any]:
    payload: Any = None
    if row.payload_json:
        with contextlib.suppress(ValueError):
            payload = json.loads(row.payload_json)
    return {
        "id": row.id,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "user_id": row.user_id,
        "account_id": row.account_id,
        "aggregate_type": row.aggregate_type,
        "aggregate_id": row.aggregate_id,
        "event_type": row.event_type,
        "payload": payload,
        "schema_version": row.schema_version,
        "source": row.source,
    }
