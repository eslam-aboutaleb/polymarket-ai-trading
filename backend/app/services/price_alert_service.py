"""AI verdicts for triggered price alerts (plan 03).

A stop-loss or take-profit crossing used to reach users as a bare,
content-free notification (and GTC events did not reach them at all).
This service asks the LLM for a verdict — hold / add / reduce / exit,
whether the move broke the original thesis, and what is driving it —
and puts that verdict in the alert payload's ``message`` field so chat
channels render it next to the numbers.

Two invariants shape the design:

* **The price-order hot loop never waits for a verdict.** Callers
  (:mod:`app.services.stop_loss_monitor`) spawn
  :func:`enrich_trigger_alert` as a fire-and-forget task per trigger: one
  gRPC call plus a research subprocess can take up to ``grpc_timeout``
  (60 s) on a loop that ticks every 10 s.
* **The alert is enriched, never gated.** When the LLM is disabled
  (``price_alert_llm_enabled=False``) or unreachable, the alert still
  fires with the plain factual message.

The in-app feed cannot render the verdict: ``NotificationFeedEvent`` has
no text column, so only label + summary + verdict metadata reach the
app. Chat and email carry the full message.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import AIBackend, get_settings
from app.grpc_clients.analysis_client import UNUSABLE_VERDICT, AnalysisClient
from app.models.user_settings import AIBackendType, UserSettings
from app.services.alert_service import dispatch as alert_dispatch
from app.services.simulation import average_entry_price
from app.utils.database import SessionLocal

logger = logging.getLogger(__name__)

# Alert event types this module enriches (both are catalog entries).
STOP_LOSS_EVENT = "stop_loss_hit"
TAKE_PROFIT_EVENT = "take_profit_hit"
PRICE_EVENT_TYPES = frozenset({STOP_LOSS_EVENT, TAKE_PROFIT_EVENT})

# Prefix used in the alert message for each event type.
_EVENT_PREFIX = {
    STOP_LOSS_EVENT: "Stop-loss",
    TAKE_PROFIT_EVENT: "Take-profit",
}

_DEFAULT_METRICS: dict[str, int] = {
    "verdicts_total": 0,
    "verdicts_failed": 0,
    "mcp_errors_total": 0,
    "alerts_enriched": 0,
    "alerts_plain": 0,
    "dispatch_failed": 0,
}
_metrics: dict[str, int] = dict(_DEFAULT_METRICS)


def _metric_inc(key: str, by: int = 1) -> None:
    _metrics[key] = _metrics.get(key, 0) + by


def get_price_alert_metrics() -> dict[str, int]:
    """Counters for the verdict / dispatch paths (observability only)."""
    return dict(_metrics)


def reset_price_alert_metrics() -> None:
    """Restore the counters to their defaults.

    The counters are process-global, so tests that assert exact values
    need a known baseline no matter which modules ran before them.
    """
    _metrics.clear()
    _metrics.update(_DEFAULT_METRICS)


def verdicts_enabled() -> bool:
    """Whether LLM verdicts may be generated (``price_alert_llm_enabled``).

    Settings read failures fail closed: the alert still fires, just
    without a verdict.
    """
    try:
        return bool(get_settings().price_alert_llm_enabled)
    except Exception:
        logger.debug("price alert settings unavailable; verdicts disabled", exc_info=True)
        return False


def _format_size(position_size: float | None) -> str:
    if position_size and position_size > 0:
        return f"{position_size:g} shares"
    return "an unrecorded position"


def _format_position(
    outcome: str,
    position_size: float | None,
    avg_price: float | None,
) -> str:
    """Render the prompt's ``{position}`` placeholder."""
    text = f"{_format_size(position_size)} of {outcome or 'the position'}"
    if avg_price and avg_price > 0:
        text += f" (avg entry ${avg_price:.4f})"
    return text


def _format_price_change(current_price: float, prev_price: float | None) -> str:
    """Signed percentage move; ``""`` when there is no reference price.

    The prompt placeholder is always supplied — an empty string keeps
    ``str.format`` from raising when the token was never priced before.
    """
    if not prev_price or prev_price <= 0:
        return ""
    return f"{((current_price - prev_price) / prev_price) * 100.0:+.1f}"


def _verdict_block(verdict: dict[str, Any] | None) -> list[str]:
    """Render the verdict lines, or nothing when the verdict is empty.

    The analysis client fails open to ``hold`` with confidence 0.0, which
    is indistinguishable from a genuine "do nothing" answer, so an empty
    verdict is reported as no verdict rather than as advice.
    """
    if not verdict:
        return []
    confidence = float(verdict.get("confidence") or 0.0)
    reasoning = str(verdict.get("reasoning") or "").strip()
    if confidence <= 0.0 and not reasoning:
        return []
    lines = [
        "AI verdict: "
        f"{str(verdict.get('verdict', 'hold')).upper()} "
        f"(confidence {confidence * 100:.0f}%, "
        f"driver {str(verdict.get('driver', 'mixed'))})",
        f"Thesis changed: {'yes' if verdict.get('thesis_changed') else 'no'}",
    ]
    if reasoning:
        lines.append(f"Reasoning: {reasoning}")
    return lines


def format_trigger_message(
    order: Any,
    *,
    event_type: str,
    live_price: float,
    trigger_condition: str,
    verdict: dict[str, Any] | None,
    simulation_mode: bool = False,
) -> str:
    """Build the ``message`` payload field chat channels render.

    ``verdict=None`` (kill switch off, or the backend was unreachable)
    yields the plain factual message — the same content users got
    before enrichment.

    ``simulation_mode`` marks a paper-trading fill: nothing was sold on
    the exchange, so the message says "simulated sell" instead of
    claiming an order that never happened.
    """
    prefix = _EVENT_PREFIX.get(event_type, event_type.replace("_", " ").title())
    market_title = str(getattr(order, "market_title", "") or "unknown market")
    outcome = str(getattr(order, "outcome", "") or "")
    subject = f"{prefix} triggered: {market_title}"
    if outcome:
        subject += f" ({outcome})"
    verb = (
        f"simulated sell of {_format_size(getattr(order, 'size', None))}"
        if simulation_mode
        else f"sold {_format_size(getattr(order, 'size', None))}"
    )
    subject += f" — {verb} @ ${live_price:.4f}"
    if trigger_condition:
        subject += f" [{trigger_condition}]"
    return "\n".join([subject, *_verdict_block(verdict)])


def build_trigger_payload(
    order: Any,
    *,
    event_type: str,
    live_price: float,
    trigger_condition: str,
    verdict: dict[str, Any] | None,
    simulation_mode: bool = False,
) -> dict[str, Any]:
    """Alert payload for a triggered price order.

    ``message`` carries the verdict text (rendered by
    ``format_alert_message``); the remaining keys populate the in-app
    feed event and the delivery row's ``payload_json``.
    """
    payload: dict[str, Any] = {
        "message": format_trigger_message(
            order,
            event_type=event_type,
            live_price=live_price,
            trigger_condition=trigger_condition,
            verdict=verdict,
            simulation_mode=simulation_mode,
        ),
        "market_id": str(getattr(order, "market_id", "") or ""),
        "token_id": str(getattr(order, "token_id", "") or ""),
        "side": "sell",
        "size": float(getattr(order, "size", 0.0) or 0.0),
        "price": live_price,
        "trigger": trigger_condition,
    }
    if simulation_mode:
        payload["simulation_mode"] = True
    if verdict:
        payload["verdict"] = str(verdict.get("verdict", "hold"))
        payload["confidence"] = float(verdict.get("confidence") or 0.0)
    return payload


def _resolve_avg_price(db: Session, order: Any) -> float | None:
    """Volume-weighted average entry for the triggered token, if known."""
    try:
        entry = average_entry_price(db, order.user_id, order.token_id)
    except Exception:
        logger.debug(
            "Average entry price unavailable for order=%s",
            getattr(order, "id", None),
            exc_info=True,
        )
        return None
    try:
        entry = float(entry) if entry is not None else None
    except (TypeError, ValueError):
        return None
    return entry if entry and entry > 0 else None


async def evaluate_trigger(
    *,
    user_id: int,
    condition_id: str,
    market_title: str,
    outcome: str,
    position_size: float | None,
    avg_price: float | None,
    trigger_condition: str,
    current_price: float,
    prev_price: float | None,
    db: Session,
) -> dict[str, Any] | None:
    """Ask the LLM for a hold / add / reduce / exit verdict.

    Returns ``None`` when no verdict can be attached — the kill switch
    is off, or the analysis backend was unreachable — so the caller
    still dispatches the plain alert. A returned dict always carries the
    prompt's five fields.

    Backend selection mirrors ``inverse_bot_monitor._evaluate_with_ai``:
    the user's ``UserSettings.ai_backend`` decides between the LangChain
    and CLI-agent backends, and every failure is counted and swallowed.
    """
    if not verdicts_enabled():
        logger.debug("price_alert_llm_enabled is off; alert fires un-enriched")
        return None

    settings_row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    backend = AIBackend.LLM_CHAIN
    if settings_row and settings_row.ai_backend == AIBackendType.CLI_AGENT.value:
        backend = AIBackend.CLI_AGENT

    client = AnalysisClient(backend=backend)
    _metric_inc("verdicts_total")
    try:
        verdict = await client.analyze_price_alert(
            market_title=market_title,
            position=_format_position(outcome, position_size, avg_price),
            trigger_condition=trigger_condition,
            current_price=f"{current_price:.4f}",
            price_change=_format_price_change(current_price, prev_price),
            condition_id=condition_id,
        )
    except Exception as exc:
        _metric_inc("mcp_errors_total")
        _metric_inc("verdicts_failed")
        logger.warning("Price-alert verdict failed (user=%s): %s", user_id, exc)
        return None
    finally:
        await client.close()

    if str(verdict.get("verdict", "")).lower() == UNUSABLE_VERDICT:
        # The response carried no usable JSON, so the client answered
        # with its fail-open defaults. That is not advice: count the
        # failure and let the alert fire plain instead of persisting a
        # "hold" that looks like a real recommendation.
        _metric_inc("verdicts_failed")
        logger.warning("Price-alert verdict unusable (user=%s)", user_id)
        return None

    logger.info(
        "Price-alert verdict: %s (thesis_changed=%s, confidence=%s)",
        verdict.get("verdict"),
        verdict.get("thesis_changed"),
        verdict.get("confidence"),
    )
    return verdict


async def dispatch_trigger_alert(
    event_type: str,
    user_id: int,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Dispatch one price alert; never raises into the background task."""
    try:
        return await alert_dispatch(event_type, user_id, payload)
    except Exception:
        _metric_inc("dispatch_failed")
        logger.warning(
            "Price-alert dispatch failed for user=%s event=%s",
            user_id,
            event_type,
            exc_info=True,
        )
        return None


async def enrich_trigger_alert(
    order: Any,
    *,
    event_type: str,
    live_price: float,
    trigger_condition: str,
    prev_price: float | None = None,
    simulation_mode: bool = False,
    enrich: bool = True,
) -> dict[str, Any] | None:
    """Generate the verdict for a trigger, then dispatch the alert.

    Runs as a fire-and-forget task owned by the calling monitor, so it
    owns its own database session: the monitor's session is closed as
    soon as the price-order cycle ends, long before a gRPC verdict can
    return. Never raises — a failed verdict or dispatch degrades to the
    plain alert and a log line.

    ``simulation_mode`` marks paper trading (no real fill happened) and
    ``enrich=False`` skips the LLM verdict when the monitor is already
    running its allowed quota of verdict tasks; the alert still fires,
    just un-enriched.
    """
    verdict: dict[str, Any] | None = None
    if enrich and verdicts_enabled():
        db: Session | None = None
        try:
            db = SessionLocal()
            verdict = await evaluate_trigger(
                user_id=order.user_id,
                condition_id=str(getattr(order, "market_id", "") or ""),
                market_title=str(getattr(order, "market_title", "") or ""),
                outcome=str(getattr(order, "outcome", "") or ""),
                position_size=float(getattr(order, "size", 0.0) or 0.0),
                avg_price=_resolve_avg_price(db, order),
                trigger_condition=trigger_condition,
                current_price=live_price,
                prev_price=prev_price,
                db=db,
            )
        except Exception:
            # ``evaluate_trigger`` already degrades its own failures; this
            # guards everything around it (session, backend lookup) so the
            # plain alert still fires.
            _metric_inc("verdicts_failed")
            logger.warning(
                "Price-alert verdict attempt failed for order=%s",
                getattr(order, "id", None),
                exc_info=True,
            )
        finally:
            if db is not None:
                db.close()

    try:
        payload = build_trigger_payload(
            order,
            event_type=event_type,
            live_price=live_price,
            trigger_condition=trigger_condition,
            verdict=verdict,
            simulation_mode=simulation_mode,
        )
    except Exception:
        # A detached or half-expired order (the monitor's session is long
        # closed by now) must not cost the user the alert itself.
        _metric_inc("dispatch_failed")
        logger.warning(
            "Price-alert payload build failed for order=%s",
            getattr(order, "id", None),
            exc_info=True,
        )
        return None

    result = await dispatch_trigger_alert(event_type, order.user_id, payload)
    _metric_inc("alerts_enriched" if verdict else "alerts_plain")
    return result
