"""Daily portfolio digest (plan 02) — one scheduled P&L report per user.

Once a day (``daily_digest_hour_utc``, UTC) this service walks every user and
delivers a single digest rendered through the ``monitoring.daily_portfolio_summary``
prompt:

* **Delivery** — chat channels go through the shared alert dispatch under the
  ``daily_digest`` event type, so per-channel preferences, storm protection and
  the in-app feed are inherited; a *direct* email is sent in addition when the
  user has an address, SMTP is configured and ``daily_digest_enabled`` is true.
  Email is deliberately not an alert channel (``CHANNEL_TYPES`` is
  telegram|discord|webhook only).
* **Idempotency** — one digest per user per UTC day. A ``daily_digest``
  ``NotificationFeedEvent`` marker row is committed *before* any rendering or
  delivery, so a crash mid-cycle can never email the same digest twice; the
  marker is removed again if assembly fails so an operator-triggered re-run can
  still retry the same day.
* **Paper mode** — users with ``simulation_mode`` are reported against their
  ``paper_balance`` (and simulated trades only) and labelled "Paper".
* **Start value** — soft dependency on plan 05: the first hourly
  ``PortfolioSnapshot`` of the UTC day when that model has landed, otherwise
  derived from today's realized P&L and labelled as derived in the header.
"""

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import AIBackend, get_settings
from app.grpc_clients.analysis_client import AnalysisClient
from app.models.notification_channel import NotificationChannel
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.stop_loss import StopLossOrder
from app.models.user import User
from app.models.user_settings import AIBackendType, UserSettings
from app.models.user_trade import UserTrade
from app.models.whale_event import WhaleEvent
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.services.alert_service import dispatch as alert_dispatch
from app.services.email_service import is_smtp_configured, send_daily_digest_email
from app.services.polymarket_service import get_polymarket_service
from app.services.simulation import get_paper_summary
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Scheduler identity (matches SCHEDULER_INTERVALS in app.utils.scheduler_lock).
SCHEDULER_NAME = "daily_digest"

# Longest stretch the digest loop sleeps without a heartbeat. The TTL a
# stale heartbeat lives for is HEARTBEAT_TTL_SECONDS (86400), so a wait
# longer than this would expire the heartbeat it is supposed to refresh.
HEARTBEAT_CHUNK_SECONDS = 3600

# Alert event type used for the shared dispatch (catalog entry owned by
# app.services.alert_service).
EVENT_TYPE = "daily_digest"

# Report defaults.
DEFAULT_HOUR_UTC = 8
EVENTS_WINDOW_HOURS = 24
POSITIONS_IN_REPORT = 25
MAX_EVENT_LINES = 20
MAX_CATALYSTS = 5
CATALYST_MARKET_SCAN = 60
MAX_MARKET_IDS = 25
# format_alert_message renders the payload verbatim, and Telegram/Discord
# truncate long messages — keep the chat body short.
CHAT_BODY_CHARS = 800

_digest_task: asyncio.Task | None = None

_metrics: dict[str, int] = {
    "cycles": 0,
    "digests_sent": 0,
    "digests_skipped": 0,
    "digests_failed": 0,
    "emails_sent": 0,
    "emails_failed": 0,
    "chat_dispatches_failed": 0,
    "llm_fallbacks": 0,
}


def _metric_inc(key: str, by: int = 1) -> None:
    _metrics[key] = _metrics.get(key, 0) + by


def get_daily_report_metrics() -> dict[str, int]:
    """Counters for the digest cycle (observability only)."""
    return dict(_metrics)


def reset_daily_report_metrics() -> None:
    """Zero the digest counters.

    They are module-level, so a digest count observed by a later test is
    the sum of every earlier test that ran a cycle. Resetting per test
    makes the assertion "this cycle counted exactly one failure" true
    regardless of execution order.
    """
    for key in _metrics:
        _metrics[key] = 0


# ── small helpers ──────────────────────────────────────────────


def _to_float(value: Any, default: float = 0.0) -> float:
    """Coerce a raw position payload value to a float.

    Position payloads are ``any``-typed upstream data: ``None``, strings and
    missing keys are all expected and must not abort the report.
    """
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_usd(value: float, *, signed: bool = False) -> str:
    if signed and value >= 0:
        return f"+${value:,.2f}"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def _position_value(position: dict[str, Any]) -> float:
    """Market value of one position (size × current price, entry fallback)."""
    size = _to_float(position.get("size"))
    price = _to_float(position.get("curPrice")) or _to_float(position.get("price"))
    if price <= 0:
        price = _to_float(position.get("avgPrice"))
    return size * price


def _position_title(position: dict[str, Any]) -> str:
    return str(
        position.get("title")
        or position.get("question")
        or position.get("market")
        or position.get("condition_id")
        or "Unknown market"
    )


def _configured_hour() -> int:
    """UTC hour the digest runs (``daily_digest_hour_utc``, default 8)."""
    try:
        hour = int(get_settings().daily_digest_hour_utc)
    except Exception:
        hour = DEFAULT_HOUR_UTC
    return hour if 0 <= hour < 24 else DEFAULT_HOUR_UTC


def _seconds_until_next_run(now: datetime | None = None) -> float:
    """Seconds until the next configured UTC hour (sleep-first loop)."""
    now = now or utc_now()
    target = now.replace(hour=_configured_hour(), minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _digest_enabled(settings_row: UserSettings | None) -> bool:
    """Per-user opt-out flag; the settings default applies without a row."""
    if settings_row is None:
        return bool(get_settings().daily_digest_enabled_default)
    value = getattr(settings_row, "daily_digest_enabled", None)
    if value is None:
        return bool(get_settings().daily_digest_enabled_default)
    return bool(value)


def _email_ready(user: User) -> bool:
    return bool(user.email and user.email.strip()) and is_smtp_configured()


def _resolve_backend(settings_row: UserSettings | None) -> AIBackend:
    """Per-user AI backend (mirrors the price-alert verdict selection)."""
    if settings_row is not None and settings_row.ai_backend == AIBackendType.CLI_AGENT.value:
        return AIBackend.CLI_AGENT
    return AIBackend.LLM_CHAIN


def _load_credentials(wallet_address: str) -> tuple[str | None, dict | None]:
    """Private key + CLOB credentials for a wallet, or ``(None, None)``."""
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        logger.warning("Digest credentials unavailable for %s", wallet_address[:12])
        return None, None
    if not stored:
        return None, None
    return stored.get("private_key"), stored.get("clob_creds")


# ── idempotency marker ────────────────────────────────────────


def _digest_marker_exists(db: Session, user_id: int, day_start: datetime) -> bool:
    """Whether a digest was already produced for this user's UTC day."""
    return (
        db.query(NotificationFeedEvent.id)
        .filter(
            NotificationFeedEvent.user_id == user_id,
            NotificationFeedEvent.event_type == EVENT_TYPE,
            NotificationFeedEvent.created_at >= day_start,
        )
        .first()
        is not None
    )


def _create_digest_marker(db: Session, user_id: int) -> NotificationFeedEvent:
    """Write the daily marker row (committed before any delivery)."""
    marker = NotificationFeedEvent(
        user_id=user_id,
        trader_wallet="",
        event_type=EVENT_TYPE,
        email_status="skipped",
    )
    db.add(marker)
    db.flush()
    return marker


def _has_active_channels(db: Session, user_id: int) -> bool:
    return (
        db.query(NotificationChannel.id)
        .filter(
            NotificationChannel.user_id == user_id,
            NotificationChannel.is_active.is_(True),
        )
        .first()
        is not None
    )


# ── data assembly ──────────────────────────────────────────────


def _realized_pnl_today(db: Session, user_id: int, day_start: datetime, *, paper: bool) -> float:
    """Sum of realized P&L booked since UTC midnight.

    Live users aggregate every strategy's closed trades; paper users count
    simulated trades only, mirroring ``get_paper_summary``.
    """
    # Simulated (paper) rows never get ``executed_at`` (only the trade
    # monitor fills that for live pending→executed hashes), so filter the
    # paper sum on ``created_at``; live sums keep ``executed_at``. Without
    # this the paper digest's realized PnL is structurally always 0.
    ts_column = UserTrade.created_at if paper else UserTrade.executed_at
    query = db.query(func.coalesce(func.sum(UserTrade.pnl), 0.0)).filter(
        UserTrade.user_id == user_id,
        ts_column >= day_start,
    )
    if paper:
        query = query.filter(UserTrade.status == "simulated")
    return float(query.scalar() or 0.0)


def _snapshot_start_value(
    db: Session, user_id: int, day_start: datetime, *, paper: bool
) -> float | None:
    """First hourly equity snapshot of the UTC day (plan 05, soft dependency).

    ``PortfolioSnapshot`` belongs to plan 05 and may not have landed yet, so
    the import is guarded; its column contract (``captured_at``, ``bankroll``,
    ``is_paper``) is defined by that plan's interface contract. ``None`` means
    "no snapshot available" and the caller derives the value instead.

    Takes the caller's session: opening (and closing) a second session here
    would detach the ORM instances the digest flow is still using.
    """
    try:
        from app.models.portfolio_snapshot import PortfolioSnapshot
    except ImportError:
        return None

    try:
        row = (
            db.query(PortfolioSnapshot.bankroll)
            .filter(
                PortfolioSnapshot.user_id == user_id,
                PortfolioSnapshot.captured_at >= day_start,
                PortfolioSnapshot.is_paper.is_(paper),
            )
            .order_by(PortfolioSnapshot.captured_at.asc())
            .first()
        )
        if row is None or row[0] is None:
            return None
        return float(row[0])
    except Exception:
        logger.warning("Equity snapshot read failed for user %d", user_id, exc_info=True)
        return None


def _resolve_start_value(
    db: Session,
    user_id: int,
    day_start: datetime,
    *,
    paper: bool,
    current_value: float,
    realized_pnl_today: float,
) -> tuple[float, bool]:
    """Opening value for the digest plus whether it had to be derived."""
    snapshot = _snapshot_start_value(db, user_id, day_start, paper=paper)
    if snapshot is not None:
        return snapshot, False
    # No hourly snapshot for this user/day: derive the opening value from
    # today's realized P&L. The derivation is labelled in the report header.
    return current_value - realized_pnl_today, True


def _market_ids(positions: list[dict[str, Any]]) -> list[str]:
    """Bounded, de-duplicated market ids used to scope whale events."""
    ids: list[str] = []
    seen: set[str] = set()
    for position in positions:
        market_id = str(position.get("condition_id") or position.get("market") or "").strip()
        if not market_id or market_id in seen:
            continue
        seen.add(market_id)
        ids.append(market_id)
        if len(ids) >= MAX_MARKET_IDS:
            break
    return ids


def _events_last_24h(
    db: Session, user_id: int, market_ids: list[str], since: datetime
) -> list[str]:
    """Human-readable events from the last ``EVENTS_WINDOW_HOURS`` hours.

    Three sources: the user's in-app feed events, the user's triggered
    stop-loss orders, and whale events on the markets the user holds (the
    whale stream is global, so it is scoped by market rather than by user).
    """
    events: list[str] = []
    try:
        feed_rows = (
            db.query(NotificationFeedEvent)
            .filter(
                NotificationFeedEvent.user_id == user_id,
                NotificationFeedEvent.created_at >= since,
            )
            .order_by(NotificationFeedEvent.created_at.desc())
            .limit(MAX_EVENT_LINES)
            .all()
        )
        events.extend(
            f"{row.created_at:%Y-%m-%d %H:%M} UTC — {row.event_type}"
            f" on {row.market_id or 'portfolio'}"
            for row in feed_rows
            if row.created_at is not None
        )

        triggered = (
            db.query(StopLossOrder)
            .filter(
                StopLossOrder.user_id == user_id,
                StopLossOrder.triggered_at >= since,
            )
            .order_by(StopLossOrder.triggered_at.desc())
            .limit(MAX_EVENT_LINES)
            .all()
        )
        events.extend(
            f"{order.triggered_at:%Y-%m-%d %H:%M} UTC — stop-loss filled on "
            f"{order.market_title or order.market_id}"
            for order in triggered
            if order.triggered_at is not None
        )

        if market_ids:
            whale_rows = (
                db.query(WhaleEvent)
                .filter(
                    WhaleEvent.market_id.in_(market_ids),
                    WhaleEvent.detected_at >= since,
                )
                .order_by(WhaleEvent.detected_at.desc())
                .limit(MAX_EVENT_LINES)
                .all()
            )
            events.extend(
                f"{row.detected_at:%Y-%m-%d %H:%M} UTC — whale {row.side or 'move'} "
                f"${_to_float(row.notional):,.0f} on {row.market_id}"
                for row in whale_rows
                if row.detected_at is not None
            )
    except Exception:
        logger.warning("Event collection failed for user %d", user_id, exc_info=True)
        return []
    return events


async def _fetch_catalysts(positions: list[dict[str, Any]]) -> list[str]:
    """Upcoming resolution dates for the user's markets.

    Sourced from the markets service (Gamma ``endDateIso``), never from the
    ``markets`` table's ``end_date`` column, which is never populated.
    """
    try:
        markets = await get_polymarket_service().get_active_markets(limit=CATALYST_MARKET_SCAN)
    except Exception:
        logger.warning("Catalyst market fetch failed", exc_info=True)
        return []

    ends: dict[str, tuple[str, str]] = {}
    for market in markets or []:
        if not isinstance(market, dict):
            continue
        condition_id = market.get("condition_id") or market.get("conditionId")
        end_date = market.get("end_date_iso") or market.get("endDateIso") or market.get("end_date")
        if not condition_id or not end_date:
            continue
        title = market.get("question") or market.get("_event_title") or str(condition_id)
        ends[str(condition_id)] = (str(title), str(end_date))
    if not ends:
        return []

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for position in positions:
        condition_id = str(position.get("condition_id") or position.get("market") or "")
        info = ends.get(condition_id)
        if info is None or condition_id in seen:
            continue
        seen.add(condition_id)
        found.append((info[1], info[0]))
        if len(found) >= MAX_CATALYSTS:
            break

    found.sort()
    return [f"{title} — resolves {end_date}" for end_date, title in found]


async def _fetch_positions(
    wallet_address: str,
    *,
    private_key: str | None,
    clob_creds: dict | None,
) -> list[dict[str, Any]]:
    """Full position list (the portfolio summary truncates it to 20)."""
    try:
        positions = await get_polymarket_service().get_positions(
            wallet_address, private_key=private_key, clob_creds=clob_creds
        )
    except Exception:
        logger.warning("Position fetch failed for %s", wallet_address[:12], exc_info=True)
        return []
    return [position for position in (positions or []) if isinstance(position, dict)]


async def _fetch_portfolio(
    wallet_address: str,
    *,
    private_key: str | None,
    clob_creds: dict | None,
) -> tuple[float, list[dict[str, Any]], int]:
    """Current value, the capped position list, and the total position count.

    The count is captured BEFORE the report cap: a 40-position user must
    read "Open positions: 40" (with the 25 biggest above the prompt) —
    reporting 25 said the other fifteen were not even held.
    """
    service = get_polymarket_service()
    current_value: float | None = None
    try:
        summary = await service.get_portfolio_summary(
            wallet_address, private_key=private_key, clob_creds=clob_creds
        )
        current_value = _to_float(summary.get("usdc_balance")) + _to_float(
            summary.get("total_current_value")
        )
    except Exception:
        logger.warning("Portfolio summary unavailable for %s", wallet_address[:12], exc_info=True)

    positions = await _fetch_positions(
        wallet_address, private_key=private_key, clob_creds=clob_creds
    )
    if current_value is None:
        current_value = sum(_position_value(position) for position in positions)
    position_count = len(positions)
    positions.sort(key=_position_value, reverse=True)
    return current_value, positions[:POSITIONS_IN_REPORT], position_count


def _positions_payload(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact, JSON-safe position rows for the prompt's ``{positions}``."""
    return [
        {
            "market": _position_title(position),
            "outcome": str(position.get("outcome") or ""),
            "size": round(_to_float(position.get("size")), 4),
            "avg_price": round(_to_float(position.get("avgPrice") or position.get("avg_price")), 4),
            "current_price": round(_to_float(position.get("curPrice") or position.get("price")), 4),
            "value_usd": round(_position_value(position), 2),
            "pnl_usd": round(_to_float(position.get("pnl")), 2),
        }
        for position in positions
    ]


def _format_events_text(events: list[str], catalysts: list[str]) -> str:
    """Events block for the prompt's ``{events}`` placeholder."""
    lines = list(events) if events else ["No notable events in the last 24 hours."]
    if catalysts:
        lines.append("")
        lines.append("Upcoming catalysts:")
        lines.extend(f"- {catalyst}" for catalyst in catalysts)
    return "\n".join(lines)


async def _assemble_digest_data(
    db: Session,
    user: User,
    *,
    paper: bool,
    now: datetime,
    day_start: datetime,
) -> dict[str, Any]:
    """Everything the digest needs; every source degrades to empty on error."""
    if paper:
        paper_summary = get_paper_summary(db, user.id)
        current_value = _to_float(paper_summary.get("paper_balance"))
        positions = await _fetch_positions(user.wallet_address, private_key=None, clob_creds=None)
        # The paper branch never caps its list, so its length is the count.
        position_count = len(positions)
    else:
        private_key, clob_creds = _load_credentials(user.wallet_address)
        current_value, positions, position_count = await _fetch_portfolio(
            user.wallet_address, private_key=private_key, clob_creds=clob_creds
        )

    realized_pnl_today = _realized_pnl_today(db, user.id, day_start, paper=paper)
    start_value, start_derived = _resolve_start_value(
        db,
        user.id,
        day_start,
        paper=paper,
        current_value=current_value,
        realized_pnl_today=realized_pnl_today,
    )
    market_ids = _market_ids(positions)
    events = _events_last_24h(db, user.id, market_ids, now - timedelta(hours=EVENTS_WINDOW_HOURS))
    catalysts = await _fetch_catalysts(positions)

    return {
        "date": day_start.strftime("%Y-%m-%d"),
        "paper": paper,
        "current_value": round(current_value, 2),
        "start_value": round(start_value, 2),
        "start_value_derived": start_derived,
        "realized_pnl_today": round(realized_pnl_today, 2),
        "positions": _positions_payload(positions),
        "position_count": position_count,
        "events": events,
        "catalysts": catalysts,
    }


# ── rendering ──────────────────────────────────────────────────


def _fallback_report(data: dict[str, Any]) -> str:
    """Deterministic digest used when the LLM article is unavailable.

    The analysis client fails open to an empty string, and a digest is still
    worth sending: the numbers and movers are factual, only the prose is
    missing.
    """
    lines = [
        "P&L today: "
        f"{_format_usd(data['realized_pnl_today'], signed=True)} "
        f"(portfolio {_format_usd(data['current_value'])}, "
        f"started {_format_usd(data['start_value'])})"
    ]
    movers = sorted(
        data["positions"], key=lambda row: abs(_to_float(row.get("pnl_usd"))), reverse=True
    )[:5]
    if movers:
        lines.append("Key movers:")
        for row in movers:
            lines.append(
                f"- {row.get('market')} ({row.get('outcome') or 'position'}): "
                f"{_format_usd(_to_float(row.get('pnl_usd')), signed=True)}, "
                f"{_to_float(row.get('size')):g} shares "
                f"@ {_to_float(row.get('current_price')):.4f}"
            )
    else:
        lines.append("No open positions.")
    if data["events"]:
        lines.append("Events (last 24h):")
        lines.extend(f"- {event}" for event in data["events"][:10])
    if data["catalysts"]:
        lines.append("Upcoming catalysts:")
        lines.extend(f"- {catalyst}" for catalyst in data["catalysts"])
    return "\n".join(lines)


async def _render_digest(data: dict[str, Any], settings_row: UserSettings | None) -> str:
    """Render the article; the deterministic report is the failure fallback."""
    try:
        client = AnalysisClient(backend=_resolve_backend(settings_row))
    except Exception:
        _metric_inc("llm_fallbacks")
        logger.warning("Analysis client unavailable for the daily digest", exc_info=True)
        return _fallback_report(data)

    try:
        article = await client.generate_daily_digest(
            date=data["date"],
            start_value=f"{data['start_value']:.2f}",
            current_value=f"{data['current_value']:.2f}",
            positions_json=json.dumps(data["positions"]),
            events=_format_events_text(data["events"], data["catalysts"]),
        )
    except Exception:
        # Only the constructor used to be guarded here: any failure from
        # the awaited call escaped to run_daily_digests' assembly-failure
        # path and burned the whole day. The deterministic report below is
        # perfectly good newsstand copy, so send that instead.
        _metric_inc("llm_fallbacks")
        logger.warning("Daily digest LLM render failed; using the deterministic report")
        return _fallback_report(data)
    finally:
        with contextlib.suppress(Exception):
            await client.close()

    if not article.strip():
        _metric_inc("llm_fallbacks")
        return _fallback_report(data)
    return article.strip()


def _chat_body(data: dict[str, Any], article: str) -> str:
    """Plain-text chat body: labels + the (truncated) article."""
    labels: list[str] = []
    if data["paper"]:
        labels.append("Paper account")
    if data["start_value_derived"]:
        labels.append("start value derived from today's realized P&L")
    text = article.strip()
    if labels:
        text = " / ".join(labels) + "\n" + text
    if len(text) > CHAT_BODY_CHARS:
        text = text[: CHAT_BODY_CHARS - 1].rstrip() + "…"
    return text


def _email_subject(data: dict[str, Any]) -> str:
    prefix = "Paper Daily Portfolio Digest" if data["paper"] else "Daily Portfolio Digest"
    return f"{prefix} - {data['date']}"


def _email_body(data: dict[str, Any], article: str) -> str:
    derived = " (derived from today's realized P&L)" if data["start_value_derived"] else ""
    return "\n".join(
        [
            f"Daily Portfolio Digest - {data['date']}" + (" (Paper)" if data["paper"] else ""),
            "",
            f"Start value: {_format_usd(data['start_value'])}{derived}",
            f"Current value: {_format_usd(data['current_value'])}",
            f"Realized P&L today: {_format_usd(data['realized_pnl_today'], signed=True)}",
            f"Open positions: {data['position_count']}",
            "",
            article,
        ]
    )


# ── delivery ───────────────────────────────────────────────────


async def _deliver(
    db: Session,
    user: User,
    marker: NotificationFeedEvent,
    *,
    data: dict[str, Any],
    article: str,
    send_email: bool,
) -> dict[str, str]:
    """Dispatch to chat channels and send the email; update the marker."""
    payload = {
        "message": _chat_body(data, article),
        "feed_event_id": marker.id,
        "skip_feed": True,
    }
    result = {"chat": "skipped", "email": "skipped"}

    try:
        dispatch_result = await alert_dispatch(EVENT_TYPE, user.id, payload)
        result["chat"] = str(dispatch_result.get("status") or "unknown")
    except Exception:
        result["chat"] = "failed"
        _metric_inc("chat_dispatches_failed")
        logger.warning("Daily digest dispatch failed for user %s", user.id, exc_info=True)

    if send_email:
        try:
            await send_daily_digest_email(
                user.email, _email_subject(data), _email_body(data, article)
            )
            result["email"] = "sent"
            _metric_inc("emails_sent")
        except Exception:
            result["email"] = "failed"
            _metric_inc("emails_failed")
            logger.warning("Daily digest email failed for user %s", user.id, exc_info=True)

    marker.email_status = result["email"] if send_email else "skipped"
    if result["email"] == "sent":
        marker.emailed_at = utc_now()
    db.commit()
    return result


async def generate_digest_for_user(
    db: Session, user: User, *, now: datetime | None = None
) -> dict[str, Any]:
    """Assemble, render and deliver one user's daily digest.

    Returns a result dict with at least a ``status`` key (``sent``,
    ``skipped`` or ``failed``); skips are explained by ``reason``.
    """
    now = now or utc_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    settings_row = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    if not _digest_enabled(settings_row):
        return {"status": "skipped", "reason": "disabled"}

    if _digest_marker_exists(db, user.id, day_start):
        return {"status": "skipped", "reason": "already_sent_today"}

    paper = bool(settings_row and settings_row.simulation_mode)
    send_email = _email_ready(user)
    if not send_email and not _has_active_channels(db, user.id):
        # No delivery target: skip silently without writing the marker, so a
        # channel or email added later today still receives the digest.
        return {"status": "skipped", "reason": "no_delivery_target"}

    marker = _create_digest_marker(db, user.id)
    db.commit()

    try:
        data = await _assemble_digest_data(db, user, paper=paper, now=now, day_start=day_start)
        article = await _render_digest(data, settings_row)
    except Exception as exc:
        # Remove the marker so an operator-triggered re-run can retry.
        logger.error("Daily digest assembly failed for user %s: %s", user.id, exc)
        with contextlib.suppress(Exception):
            db.rollback()
            db.delete(marker)
            db.commit()
        return {"status": "failed", "reason": "assembly_failed"}

    delivery = await _deliver(db, user, marker, data=data, article=article, send_email=send_email)
    _metric_inc("digests_sent")
    logger.info("Daily digest delivered for user %s: %s", user.id, delivery)
    return {"status": "sent", **delivery}


async def run_daily_digests() -> dict[str, int]:
    """One digest cycle for every user (idempotent per UTC day)."""
    stats = {"users": 0, "sent": 0, "skipped": 0, "failed": 0}
    db = SessionLocal()
    try:
        users = db.query(User).all()
        # Snapshot the ids first: an inner rollback can detach/expire the
        # ORM instances, and neither logging nor the retry path may raise
        # DetachedInstanceError while walking the rest of the users.
        user_ids = [u.id for u in users]
        for user_id in user_ids:
            stats["users"] += 1
            try:
                user = db.get(User, user_id)
                result = await generate_digest_for_user(db, user)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stats["failed"] += 1
                _metric_inc("digests_failed")
                logger.error("Daily digest failed for user %d: %s", user_id, exc)
                with contextlib.suppress(Exception):
                    db.rollback()
                continue
            if result.get("status") == "sent":
                stats["sent"] += 1
                logger.info(
                    "Daily digest for user %d: sent (chat=%s email=%s)",
                    user_id,
                    result.get("chat"),
                    result.get("email"),
                )
            elif result.get("status") == "failed":
                # An assembly failure is a failure, not a skip: folding it
                # into "skipped" hid the night's damage in the counters.
                stats["failed"] += 1
                _metric_inc("digests_failed")
                logger.warning("Daily digest for user %d failed: %s", user_id, result.get("reason"))
            else:
                stats["skipped"] += 1
                logger.info("Daily digest for user %d: %s", user_id, result.get("reason"))
    finally:
        db.close()
    _metric_inc("cycles")
    return stats


# ── background loop ────────────────────────────────────────────


async def _daily_digest_loop() -> None:
    """Sleep-first daily loop with the scheduler lock and heartbeat.

    The sleep comes before the first cycle (``news_service`` idiom) so a fresh
    boot does not fire one LLM call per user immediately; the advisory lock
    and heartbeat follow ``position_lifecycle_service``.
    """
    if not acquire_scheduler_lock(SCHEDULER_NAME):
        return
    logger.info("Daily report service started (runs at %02d:00 UTC)", _configured_hour())
    try:
        while True:
            # Sleep-first so the digest lands on the configured hour
            # instead of once per startup. The wait is chunked with a
            # heartbeat per chunk: ``HEARTBEAT_TTL_SECONDS`` is 86400 —
            # exactly one day — so an un-chunked 24h sleep expires the
            # previous heartbeat at the very moment the next write
            # happens and a dead digest job is silent forever.
            deadline = utc_now() + timedelta(seconds=_seconds_until_next_run())
            while True:
                remaining = (deadline - utc_now()).total_seconds()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(remaining, HEARTBEAT_CHUNK_SECONDS))
                scheduler_heartbeat(SCHEDULER_NAME)
            try:
                await run_daily_digests()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Daily digest cycle failed: %s", exc)
            scheduler_heartbeat(SCHEDULER_NAME)
    finally:
        release_scheduler_lock(SCHEDULER_NAME)
        logger.info("Daily report service stopped")


async def start_daily_report_service() -> None:
    """Start the daily digest background task (main.py lifespan hook)."""
    global _digest_task
    if _digest_task is not None and not _digest_task.done():
        logger.warning("Daily report service already running")
        return
    _digest_task = asyncio.create_task(_daily_digest_loop())
    logger.info("Daily report service started")


async def stop_daily_report_service() -> None:
    """Stop the daily digest background task (main.py lifespan hook)."""
    global _digest_task
    if _digest_task is not None and not _digest_task.done():
        _digest_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _digest_task
    _digest_task = None
    logger.info("Daily report service stopped")
