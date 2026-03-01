"""
Inverse Position Bot monitor.

Evaluates configured positions every 5 minutes and auto-reverses when
guardrails are satisfied.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, date
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import AIBackend
from app.grpc_clients.analysis_client import AnalysisClient
from app.models.inverse_bot_action import InverseBotAction
from app.models.inverse_bot_position import InverseBotPosition
from app.models.user import User
from app.models.user_settings import InverseBotSizeMode, UserSettings, AIBackendType
from app.services.copy_trade_service import _place_order_on_polymarket
from app.services.polymarket_service import POLYMARKET_CLOB_API, get_polymarket_service
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.utils.database import SessionLocal
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

INVERSE_BOT_CHECK_INTERVAL = 300  # 5 minutes
INVERSE_DELTA_TRIGGER = 0.08
INVERSE_PERSISTENCE_REQUIRED = 2
BUY_SLIPPAGE_GUARD = 0.015  # 1.5%
MIN_BUY_NOTIONAL = 1.0

_monitor_task: asyncio.Task | None = None
_inflight_keys: set[str] = set()
_metrics: dict[str, int] = {
    "evaluations_total": 0,
    "reversals_total": 0,
    "reversals_failed": 0,
    "sell_only_total": 0,
    "mcp_errors_total": 0,
}
_last_tick_at: datetime | None = None


def _metric_inc(key: str, by: int = 1):
    _metrics[key] = _metrics.get(key, 0) + by


def get_inverse_bot_metrics() -> dict[str, Any]:
    return {
        **_metrics,
        "running": bool(_monitor_task and not _monitor_task.done()),
        "last_tick_at": _last_tick_at.isoformat() if _last_tick_at else None,
        "inflight": len(_inflight_keys),
        "interval_seconds": INVERSE_BOT_CHECK_INTERVAL,
    }


async def start_inverse_bot_monitor():
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        logger.info("Inverse-bot monitor already running")
        return
    _monitor_task = asyncio.create_task(_monitor_loop())
    logger.info("Inverse-bot monitor started (interval=%ss)", INVERSE_BOT_CHECK_INTERVAL)


async def stop_inverse_bot_monitor():
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    _monitor_task = None
    logger.info("Inverse-bot monitor stopped")


async def _monitor_loop():
    global _last_tick_at
    while True:
        try:
            _last_tick_at = datetime.now(timezone.utc)
            await _run_monitor_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Inverse-bot monitor cycle failed: %s", e, exc_info=True)
        await asyncio.sleep(INVERSE_BOT_CHECK_INTERVAL)


async def _run_monitor_cycle():
    db = SessionLocal()
    try:
        user_ids = [
            uid for (uid,) in db.query(UserSettings.user_id).filter(
                UserSettings.inverse_bot_enabled == True,
            ).all()
        ]
        for user_id in user_ids:
            await _evaluate_user_positions(db, user_id)
    finally:
        db.close()


async def _evaluate_user_positions(db: Session, user_id: int):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return

    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings or not settings.inverse_bot_enabled:
        return

    entries = (
        db.query(InverseBotPosition)
        .filter(
            InverseBotPosition.user_id == user_id,
            InverseBotPosition.enabled == True,
        )
        .all()
    )
    if not entries:
        return

    try:
        stored = load_wallet_credentials(user.wallet_address)
    except CredentialStoreError as exc:
        logger.warning("Inverse-bot credential store unavailable for user %s: %s", user_id, exc)
        return
    if not stored:
        for row in entries:
            row.status = "error"
            row.last_error = "No trading credentials found. Re-login required."
            row.last_evaluated_at = utc_now()
        db.commit()
        return

    service = get_polymarket_service()
    try:
        positions = await service.get_positions(
            user.wallet_address,
            private_key=stored.get("private_key"),
            clob_creds=stored.get("clob_creds"),
        )
    except Exception as e:
        logger.warning("Inverse-bot positions fetch failed for user %s: %s", user_id, e)
        return

    positions_by_token = {p.get("asset_id"): p for p in positions if p.get("asset_id")}

    for row in entries:
        lock_key = f"{user_id}:{row.condition_id}"
        if lock_key in _inflight_keys:
            continue
        _inflight_keys.add(lock_key)
        try:
            await evaluate_inverse_position_row(
                db=db,
                user=user,
                settings=settings,
                row=row,
                stored_creds=stored,
                positions_by_token=positions_by_token,
                force=False,
            )
        finally:
            _inflight_keys.discard(lock_key)


async def manual_evaluate_inverse_position(
    db: Session,
    user_id: int,
    row_id: int,
) -> dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"success": False, "detail": "User not found"}
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        return {"success": False, "detail": "User settings not found"}
    row = db.query(InverseBotPosition).filter(
        InverseBotPosition.id == row_id,
        InverseBotPosition.user_id == user_id,
    ).first()
    if not row:
        return {"success": False, "detail": "Inverse bot position not found"}

    try:
        stored = load_wallet_credentials(user.wallet_address)
    except CredentialStoreError as exc:
        return {"success": False, "detail": f"Credential store unavailable: {exc}"}
    service = get_polymarket_service()
    positions = []
    if stored:
        positions = await service.get_positions(
            user.wallet_address,
            private_key=stored.get("private_key"),
            clob_creds=stored.get("clob_creds"),
        )
    positions_by_token = {p.get("asset_id"): p for p in positions if p.get("asset_id")}
    result = await evaluate_inverse_position_row(
        db=db,
        user=user,
        settings=settings,
        row=row,
        stored_creds=stored,
        positions_by_token=positions_by_token,
        force=True,
    )
    return {"success": True, "result": result}


async def evaluate_inverse_position_row(
    db: Session,
    user: User,
    settings: UserSettings,
    row: InverseBotPosition,
    stored_creds: Optional[dict],
    positions_by_token: dict[str, dict],
    force: bool = False,
) -> dict[str, Any]:
    _metric_inc("evaluations_total")
    now = datetime.now(timezone.utc)
    previous_signal = row.last_signal
    row.last_evaluated_at = utc_now()
    row.last_error = None

    current_pos = positions_by_token.get(row.token_id)
    if not current_pos:
        row.enabled = False
        row.status = "error"
        row.last_error = "Position closed or token no longer held."
        db.commit()
        return {"status": "disabled", "reason": row.last_error}

    size = float(current_pos.get("size") or 0.0)
    if size <= 0:
        row.enabled = False
        row.status = "error"
        row.last_error = "Invalid position size."
        db.commit()
        return {"status": "disabled", "reason": row.last_error}

    market = await _fetch_market_tokens(row.condition_id)
    if not market or not market.get("tokens"):
        row.status = "error"
        row.last_error = "Unable to fetch market token distribution."
        db.commit()
        return {"status": "error", "reason": row.last_error}

    tokens = market["tokens"]
    held = next((t for t in tokens if t["token_id"] == row.token_id), None)
    if not held:
        row.status = "error"
        row.last_error = "Held token not found in market."
        db.commit()
        return {"status": "error", "reason": row.last_error}

    alternatives = [t for t in tokens if t["token_id"] != row.token_id and t["price"] > 0]
    if not alternatives:
        row.status = "error"
        row.last_error = "No tradable alternative token found."
        db.commit()
        return {"status": "error", "reason": row.last_error}

    held_pct = held["price"]
    best_alt = max(alternatives, key=lambda t: t["price"])
    best_alt_pct = best_alt["price"]
    delta_pct = best_alt_pct - held_pct

    row.last_signal = f"delta={delta_pct:.4f}"
    if delta_pct < INVERSE_DELTA_TRIGGER:
        row.persistence_count = 0
        row.status = "active"
        row.last_recommendation = "hold"
        row.last_reasoning = (
            f"Delta below trigger: best alternative {best_alt_pct:.3f} vs held {held_pct:.3f}."
        )
        db.commit()
        return {"status": "hold", "delta_pct": delta_pct}

    ai_eval = await _evaluate_with_ai(
        db=db,
        user_id=user.id,
        condition_id=row.condition_id,
        market_title=row.market_title or (current_pos.get("title") or ""),
        held_outcome=row.outcome or held["outcome"],
        held_pct=held_pct,
        best_alt_outcome=best_alt["outcome"],
        best_alt_pct=best_alt_pct,
        delta_pct=delta_pct,
        alternatives=alternatives,
    )

    recommendation = str(ai_eval.get("recommendation", "hold")).lower()
    confidence = float(ai_eval.get("confidence", 0.0))
    row.last_confidence = confidence
    row.last_recommendation = recommendation
    row.last_reasoning = ai_eval.get("reasoning", "")
    row.last_web_summary = ai_eval.get("web_summary", "")
    row.last_x_summary = ai_eval.get("x_summary", "")
    row.last_alt_outcome = ai_eval.get("alt_outcome", best_alt["outcome"])
    row.last_alt_token_id = ai_eval.get("alt_token_id", best_alt["token_id"])

    if recommendation != "reverse":
        row.persistence_count = 0
        row.status = "active"
        db.commit()
        return {"status": "hold", "reason": "AI recommendation not reverse"}

    if confidence < float(settings.inverse_bot_confidence_threshold or 75):
        row.persistence_count = 0
        row.status = "active"
        db.commit()
        return {"status": "hold", "reason": "Confidence below threshold"}

    reverse_token = _resolve_alt_token(tokens, ai_eval, best_alt)
    reverse_signal = f"reverse:{reverse_token['token_id']}"
    row.persistence_count = row.persistence_count + 1 if previous_signal == reverse_signal else 1
    row.last_signal = reverse_signal
    if row.persistence_count < INVERSE_PERSISTENCE_REQUIRED and not force:
        row.status = "active"
        db.commit()
        return {"status": "pending_persistence", "count": row.persistence_count}

    cooldown_min = int(settings.inverse_bot_cooldown_minutes or 30)
    if row.last_reversed_at:
        seconds_since = (now - row.last_reversed_at.replace(tzinfo=timezone.utc)).total_seconds()
        if seconds_since < cooldown_min * 60 and not force:
            row.status = "cooldown"
            db.commit()
            return {"status": "cooldown", "seconds_remaining": cooldown_min * 60 - seconds_since}

    today = date.today()
    if row.reversals_day != today:
        row.reversals_day = today
        row.reversals_today = 0
    if row.reversals_today >= int(settings.inverse_bot_max_reversals_per_day or 3) and not force:
        row.status = "cooldown"
        db.commit()
        return {"status": "daily_cap_reached"}

    if not stored_creds:
        row.status = "error"
        row.last_error = "No trading credentials found. Re-login required."
        db.commit()
        return {"status": "error", "reason": row.last_error}

    execution = _execute_reversal(
        row=row,
        settings=settings,
        stored_creds=stored_creds,
        from_size=size,
        from_price=held_pct,
        to_token_id=reverse_token["token_id"],
        to_outcome=reverse_token["outcome"],
        to_price=reverse_token["price"],
        confidence=confidence,
        recommendation=recommendation,
    )

    action = InverseBotAction(
        inverse_bot_position_id=row.id,
        user_id=user.id,
        condition_id=row.condition_id,
        from_token_id=row.token_id,
        to_token_id=reverse_token["token_id"],
        from_outcome=row.outcome or held["outcome"],
        to_outcome=reverse_token["outcome"],
        sell_order_hash=execution.get("sell_order_hash"),
        buy_order_hash=execution.get("buy_order_hash"),
        sell_size=size,
        buy_notional=execution.get("buy_notional"),
        confidence=confidence,
        recommendation=recommendation,
        status=execution.get("status", "failed"),
        error=execution.get("error"),
        executed_at=utc_now() if execution.get("status") in ("success", "sell_only") else None,
    )
    db.add(action)

    if execution.get("status") == "success":
        row.status = "active"
        row.token_id = reverse_token["token_id"]
        row.outcome = reverse_token["outcome"]
        row.last_reversed_at = utc_now()
        row.reversals_today = int(row.reversals_today or 0) + 1
        row.persistence_count = 0
        _metric_inc("reversals_total")
    elif execution.get("status") == "sell_only":
        row.status = "sell_only"
        row.enabled = False
        row.last_reversed_at = utc_now()
        row.reversals_today = int(row.reversals_today or 0) + 1
        row.last_error = execution.get("error")
        row.persistence_count = 0
        _metric_inc("sell_only_total")
    else:
        row.status = "error"
        row.last_error = execution.get("error")
        _metric_inc("reversals_failed")

    db.commit()
    db.refresh(action)

    return {
        "status": execution.get("status", "failed"),
        "action_id": action.id,
        "sell_order_hash": execution.get("sell_order_hash"),
        "buy_order_hash": execution.get("buy_order_hash"),
        "buy_notional": execution.get("buy_notional"),
    }


async def _fetch_market_tokens(condition_id: str) -> dict[str, Any]:
    url = f"{POLYMARKET_CLOB_API}/markets/{condition_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {}
            payload = resp.json()
            tokens = []
            for t in payload.get("tokens", []):
                token_id = str(t.get("token_id", ""))
                if not token_id:
                    continue
                try:
                    price = float(t.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                tokens.append({
                    "token_id": token_id,
                    "outcome": str(t.get("outcome") or ""),
                    "price": price,
                })
            return {"tokens": tokens, "question": payload.get("question", "")}
    except Exception:
        return {}


def _resolve_alt_token(tokens: list[dict], ai_eval: dict, best_alt: dict) -> dict:
    alt_token_id = str(ai_eval.get("alt_token_id", "") or "").strip()
    if alt_token_id:
        found = next((t for t in tokens if t["token_id"] == alt_token_id), None)
        if found:
            return found
    alt_outcome = str(ai_eval.get("alt_outcome", "") or "").lower()
    if alt_outcome:
        found = next((t for t in tokens if t["outcome"].lower() == alt_outcome), None)
        if found:
            return found
    return best_alt


def _best_ask_for_token(token_id: str) -> float:
    try:
        url = f"{POLYMARKET_CLOB_API}/book"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params={"token_id": token_id})
            if resp.status_code != 200:
                return 0.0
            data = resp.json()
            asks = data.get("asks", [])
            if not asks:
                return 0.0
            best = min(float(a.get("price", 1.0)) for a in asks if a.get("price") is not None)
            return max(0.0, min(1.0, best))
    except Exception:
        return 0.0


def _resolve_size_mode(row: InverseBotPosition, settings: UserSettings) -> str:
    mode = (row.size_mode_override or "inherit").lower()
    if mode == "inherit":
        return (settings.inverse_bot_default_size_mode or InverseBotSizeMode.FULL_NOTIONAL.value).lower()
    return mode


def _execute_reversal(
    row: InverseBotPosition,
    settings: UserSettings,
    stored_creds: dict,
    from_size: float,
    from_price: float,
    to_token_id: str,
    to_outcome: str,
    to_price: float,
    confidence: float,
    recommendation: str,
) -> dict[str, Any]:
    private_key = stored_creds.get("private_key")
    clob_creds = stored_creds.get("clob_creds")

    sell = _place_order_on_polymarket(
        private_key=private_key,
        clob_creds=clob_creds,
        token_id=row.token_id,
        side="SELL",
        price=max(0.001, min(0.999, from_price)),
        size=from_size,
    )
    if not sell.get("success"):
        return {
            "status": "failed",
            "error": sell.get("error", "SELL leg failed"),
            "sell_order_hash": sell.get("order_hash"),
            "buy_order_hash": None,
            "buy_notional": None,
        }

    size_mode = _resolve_size_mode(row, settings)
    if size_mode == InverseBotSizeMode.FIXED_AMOUNT.value:
        buy_notional = float(
            row.fixed_amount_override
            if row.fixed_amount_override and row.fixed_amount_override > 0
            else settings.inverse_bot_fixed_amount or 50.0
        )
    else:
        buy_notional = max(MIN_BUY_NOTIONAL, (from_size * from_price) * (1.0 - BUY_SLIPPAGE_GUARD))

    if buy_notional < MIN_BUY_NOTIONAL:
        return {
            "status": "sell_only",
            "error": f"Buy notional below minimum (${buy_notional:.4f}) after sell.",
            "sell_order_hash": sell.get("order_hash"),
            "buy_order_hash": None,
            "buy_notional": buy_notional,
        }

    best_ask = _best_ask_for_token(to_token_id)
    ref_price = best_ask if best_ask > 0 else max(0.001, min(0.999, to_price))
    marketable_price = min(0.999, ref_price * (1.0 + BUY_SLIPPAGE_GUARD))

    buy = _place_order_on_polymarket(
        private_key=private_key,
        clob_creds=clob_creds,
        token_id=to_token_id,
        side="BUY",
        price=marketable_price,
        size=buy_notional,
    )
    if not buy.get("success"):
        return {
            "status": "sell_only",
            "error": buy.get("error", "BUY leg failed after SELL"),
            "sell_order_hash": sell.get("order_hash"),
            "buy_order_hash": buy.get("order_hash"),
            "buy_notional": buy_notional,
        }

    return {
        "status": "success",
        "error": None,
        "sell_order_hash": sell.get("order_hash"),
        "buy_order_hash": buy.get("order_hash"),
        "buy_notional": buy_notional,
    }


async def _evaluate_with_ai(
    db: Session,
    user_id: int,
    condition_id: str,
    market_title: str,
    held_outcome: str,
    held_pct: float,
    best_alt_outcome: str,
    best_alt_pct: float,
    delta_pct: float,
    alternatives: list[dict],
) -> dict[str, Any]:
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    backend = AIBackend.LLM_CHAIN
    if settings and settings.ai_backend == AIBackendType.CLI_AGENT.value:
        backend = AIBackend.CLI_AGENT

    client = AnalysisClient(backend=backend)
    try:
        result = await client.evaluate_inverse_position(
            condition_id=condition_id,
            market_title=market_title,
            held_outcome=held_outcome,
            held_pct=held_pct,
            best_alt_outcome=best_alt_outcome,
            best_alt_pct=best_alt_pct,
            delta_pct=delta_pct,
            alternatives_json=alternatives,
            include_research=True,
            user_id=str(user_id),
        )
    except Exception as e:
        _metric_inc("mcp_errors_total")
        logger.warning("Inverse-bot AI evaluation failed (user=%s): %s", user_id, e)
        result = {}
    finally:
        await client.close()

    recommendation = str(result.get("recommendation", "hold")).lower()
    confidence = float(result.get("confidence", 0.0))
    if not result.get("x_summary"):
        # Conservative cap when X signal is unavailable
        confidence = min(confidence, 70.0)
    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "reasoning": result.get("reasoning") or result.get("analysis", ""),
        "key_risks": result.get("key_risks", []),
        "alt_outcome": result.get("alt_outcome", best_alt_outcome),
        "alt_token_id": result.get("alt_token_id", ""),
        "web_summary": result.get("web_summary", ""),
        "x_summary": result.get("x_summary", ""),
    }
