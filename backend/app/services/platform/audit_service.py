"""Audit read model and export over ``app_events`` (plan 06 Phase B).

``AppEvent`` is the single audit source (plan 10 Phase A decision 4): it
is append-only, it already carries the money-path events, and a second
audit table would only ever disagree with it. This module therefore
adds no writes of its own beyond the thin ``record_*`` emitters for
actor-initiated actions, and reads everything through
``app.services.platform.event_log.get_events`` rather than re-implementing that
query.

Invariants:

* **Read-only over the log.** Nothing here updates or deletes an
  ``AppEvent``; the append-only contract in ``event_log`` is preserved.
* **Emissions ride the caller's transaction.** Every ``record_*``
  helper appends on the session the route already owns, so an event is
  durable exactly when the action it describes is, and a rolled-back
  action leaves no event behind.
* **Emissions never break the action.** A failure to append is logged
  and swallowed: a missing audit row is a degraded trail, not a failed
  trade or a failed login.
* **No secrets, no PII.** Payloads carry ids, names of changed settings
  and amounts — never key material, never a wallet address.

Events that already exist before Phase B (emitted by
``trade_recorder`` and ``trade_monitor``): ``order_submitted``,
``order_fill_observed``, ``order_failed``, ``order_rejected_by_gate``.
Phase B adds the actor-side vocabulary below.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.platform.event_log import ACTOR_EVENTS, TRADE_EVENTS, get_events

logger = logging.getLogger(__name__)

#: Every event type the audit export understands. Trading events come
#: from ``event_log.TRADE_EVENTS``; the actor events are Phase B's
#: additions, plus the API-key lifecycle emitted by the management
#: routes.
API_KEY_EVENTS = ("api_key_created", "api_key_revoked")
AUDIT_EVENT_TYPES = tuple(sorted(TRADE_EVENTS + ACTOR_EVENTS + API_KEY_EVENTS))

#: Column order of the CSV export. Fixed so a compliance consumer can
#: diff two exports without re-reading the header.
CSV_COLUMNS = (
    "id",
    "occurred_at",
    "user_id",
    "account_id",
    "aggregate_type",
    "aggregate_id",
    "event_type",
    "source",
    "schema_version",
    "payload",
)

#: ``get_events`` filters on user and time but not on account, so an
#: account-scoped export reads this many candidate rows and filters in
#: Python. Bounded by ``get_events``' own 1000-row cap.
ACCOUNT_FILTER_FETCH_LIMIT = 1000
#: Default page size of an export.
DEFAULT_EXPORT_LIMIT = 500
MAX_EXPORT_LIMIT = 1000

#: Cells starting with one of these are formula payloads in a
#: spreadsheet. They are neutralised rather than dropped.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


# ── Emitters (actor-side vocabulary) ────────────────────────


def _emit(
    db: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | int | None,
    user_id: int | None,
    payload: dict[str, Any] | None = None,
    source: str,
) -> None:
    """Append one event, swallowing any failure.

    ``event_log.append`` already guards its own errors; this wrapper
    keeps the call sites to one line each and guarantees the audit
    surface can never raise into a route.
    """
    try:
        from app.services.platform.event_log import append

        append(
            db,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            user_id=user_id,
            payload=payload,
            source=source,
        )
    except Exception:  # pragma: no cover - append does not raise
        logger.warning("Failed to append %s event", event_type, exc_info=True)


def record_account_created(db: Session, *, account, user_id: int) -> None:
    """A new trading boundary exists (plan 06 Phase A surface)."""
    _emit(
        db,
        event_type="account_created",
        aggregate_type="account",
        aggregate_id=account.id,
        user_id=user_id,
        payload={
            "account_name": account.name,
            "kind": account.kind,
            "is_default": bool(account.is_default),
        },
        source="accounts_api",
    )


def record_account_switched(db: Session, *, user_id: int, account_id: int) -> None:
    """A caller explicitly acted as an account other than the default."""
    _emit(
        db,
        event_type="account_switched",
        aggregate_type="account",
        aggregate_id=account_id,
        user_id=user_id,
        payload={"account_id": account_id},
        source="accounts_api",
    )


def record_settings_changed(
    db: Session,
    *,
    user_id: int,
    account_id: int | None,
    changed_fields: list[str],
) -> None:
    """Settings that govern risk behaviour changed.

    Only field *names* are recorded, never values: a settings payload is
    the difference between "the daily loss limit changed" and a copy of
    the user's whole risk posture in the audit log.
    """
    if not changed_fields:
        return
    _emit(
        db,
        event_type="settings_changed",
        aggregate_type="settings",
        aggregate_id=account_id if account_id is not None else user_id,
        user_id=user_id,
        payload={
            "account_id": account_id,
            "changed_fields": sorted(changed_fields),
        },
        source="settings_api",
    )


def record_strategy_toggled(
    db: Session,
    *,
    user_id: int,
    strategy: str,
    enabled: bool,
    account_id: int | None = None,
) -> None:
    """A strategy was turned on or off.

    Emitted by the settings surfaces that own the strategy flags; kept
    here so every writer uses one event type and one payload shape.
    """
    _emit(
        db,
        event_type="strategy_enabled" if enabled else "strategy_disabled",
        aggregate_type="strategy",
        aggregate_id=strategy,
        user_id=user_id,
        payload={"strategy": strategy, "enabled": enabled, "account_id": account_id},
        source="settings_api",
    )


def record_api_key_created(db: Session, *, record, user_id: int) -> None:
    """An external-API credential was minted."""
    _emit(
        db,
        event_type="api_key_created",
        aggregate_type="api_key",
        aggregate_id=record.id,
        user_id=user_id,
        payload={
            "key_prefix": record.key_prefix,
            "name": record.name,
            "scopes": record.scopes,
            "account_id": record.account_id,
        },
        source="api_keys_api",
    )


def record_api_key_revoked(db: Session, *, record, user_id: int) -> None:
    """An external-API credential was retired."""
    _emit(
        db,
        event_type="api_key_revoked",
        aggregate_type="api_key",
        aggregate_id=record.id,
        user_id=user_id,
        payload={"key_prefix": record.key_prefix, "name": record.name},
        source="api_keys_api",
    )


# ── Read model ──────────────────────────────────────────────


def parse_moment(raw: str | None) -> Any:
    """Parse an ISO-8601 timestamp, assuming UTC when no offset is given.

    Unparseable input is ignored rather than raised: a bad query string
    must not turn a compliance export into a 500.
    """
    from datetime import UTC, datetime

    if not raw or not str(raw).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_event_types(raw: str | None) -> list[str] | None:
    """Split and validate a ``?type=a,b`` filter against the vocabulary.

    ``None`` means "no filter"; an empty result after filtering means
    "everything". Unknown names are dropped rather than rejected so a
    newer client cannot 400 an older deployment, but a filter that
    names nothing known returns the unfiltered log instead of nothing.
    """
    if raw is None or not str(raw).strip():
        return None
    requested = [part.strip() for part in str(raw).split(",") if part.strip()]
    if not requested:
        return None
    known = [event_type for event_type in requested if event_type in AUDIT_EVENT_TYPES]
    return known or None


def get_audit_events(
    db: Session,
    *,
    user_id: int | None = None,
    account_id: int | None = None,
    since: Any = None,
    until: Any = None,
    event_types: list[str] | None = None,
    aggregate_type: str | None = None,
    limit: int = DEFAULT_EXPORT_LIMIT,
) -> list[dict[str, Any]]:
    """Read audit events newest-first through ``event_log.get_events``.

    ``get_events`` has no ``account_id`` parameter, so an account filter
    is applied in Python over a bounded candidate page; a user filter is
    pushed into the query where it belongs. The result is a list of
    plain dicts — no ORM instance leaves this module.
    """
    if account_id is not None:
        # No account filter exists in ``get_events``, so the candidate
        # page is bounded and the account filter is applied here.
        fetch_limit = ACCOUNT_FILTER_FETCH_LIMIT
    else:
        fetch_limit = max(1, min(int(limit), MAX_EXPORT_LIMIT))
    rows = get_events(
        db,
        user_id=user_id,
        aggregate_type=aggregate_type,
        event_types=event_types,
        since=since,
        until=until,
        limit=fetch_limit,
    )
    if account_id is not None:
        rows = [row for row in rows if row.get("account_id") == account_id]
    return rows[: max(1, min(int(limit), MAX_EXPORT_LIMIT))]


def to_csv(rows: list[dict[str, Any]]) -> str:
    """Render audit rows as CSV.

    Payload is embedded as a JSON string (one cell, one event) and every
    cell is formula-guarded, because the export is most often opened in
    a spreadsheet.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        payload = row.get("payload")
        encoded = json.dumps(payload, default=str) if payload is not None else ""
        writer.writerow(
            [
                _csv_cell(row.get("id")),
                _csv_cell(row.get("occurred_at")),
                _csv_cell(row.get("user_id")),
                _csv_cell(row.get("account_id")),
                _csv_cell(row.get("aggregate_type")),
                _csv_cell(row.get("aggregate_id")),
                _csv_cell(row.get("event_type")),
                _csv_cell(row.get("source")),
                _csv_cell(row.get("schema_version")),
                _csv_cell(encoded),
            ]
        )
    return buffer.getvalue()


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{text}"
    return text
