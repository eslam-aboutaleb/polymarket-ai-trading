"""
Trade monitor – watches followed traders' activity via WebSocket + HTTP polling.

Uses a dual strategy:
  1. WebSocket: Connect to Polymarket's CLOB WebSocket for real-time trade events.
  2. HTTP polling: Fallback that polls CLOB /data/trades for each watched wallet.

When a new trade is detected the monitor:
  - Records it in trade_history
  - Calls the LLM analysis pipeline (if enabled)
  - Triggers copy-trade execution for each subscribing user
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Dict, Set, List, Optional, Any

import httpx

from sqlalchemy.orm import Session

from app.models.followed_trader import FollowedTrader
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.notification_followed_trader import NotificationFollowedTrader
from app.models.trade_history import TradeHistory
from app.models.market import Market
from app.models.trader_position_state import TraderPositionState
from app.models.user import User
from app.models.user_settings import UserSettings
from app.config import get_settings
from app.services.email_service import is_smtp_configured, send_follow_event_email
from app.utils.database import SessionLocal
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

POLYMARKET_CLOB_API = "https://clob.polymarket.com"
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
try:
    _settings = get_settings()
    _POLL_MAX_CONCURRENCY = max(1, int(_settings.trade_poll_max_concurrency))
    _POLL_HTTP_MAX_CONNECTIONS = max(1, int(_settings.trade_poll_http_max_connections))
    _POLL_HTTP_KEEPALIVE = max(1, int(_settings.trade_poll_http_keepalive_connections))
except Exception:
    _POLL_MAX_CONCURRENCY = 50
    _POLL_HTTP_MAX_CONNECTIONS = 200
    _POLL_HTTP_KEEPALIVE = 50

# In-memory state
_watched_wallets: Set[str] = set()
_last_seen_trades: Dict[str, str] = {}  # wallet → last trade id
_monitor_task: Optional[asyncio.Task] = None
_poll_http_client: Optional[httpx.AsyncClient] = None
_poll_semaphore: asyncio.Semaphore = asyncio.Semaphore(_POLL_MAX_CONCURRENCY)
POSITION_EPSILON = 1e-9


# ────────────── Wallet watch list management ──────────────

def refresh_watched_wallets(db: Session):
    """Rebuild the set of wallets being followed by any active user."""
    global _watched_wallets
    copy_rows = (
        db.query(FollowedTrader.trader_wallet)
        .filter(FollowedTrader.is_active == True)
        .distinct()
        .all()
    )
    notif_rows = (
        db.query(NotificationFollowedTrader.trader_wallet)
        .filter(NotificationFollowedTrader.is_active == True)
        .distinct()
        .all()
    )
    _watched_wallets = {
        r[0].lower()
        for r in (copy_rows + notif_rows)
        if r and r[0]
    }
    logger.info("Watch list refreshed: %d unique wallets", len(_watched_wallets))


# ────────────── HTTP polling (reliable fallback) ──────────────

async def _poll_trader_trades(wallet: str) -> List[Dict]:
    """Fetch recent trades for a wallet via CLOB public API."""
    try:
        client = _poll_http_client
        if client is None:
            async with httpx.AsyncClient(timeout=15.0) as temp_client:
                resp = await temp_client.get(
                    f"{POLYMARKET_CLOB_API}/data/trades",
                    params={"user": wallet, "limit": 20},
                )
        else:
            resp = await client.get(
                f"{POLYMARKET_CLOB_API}/data/trades",
                params={"user": wallet, "limit": 20},
            )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else data.get("trades", data.get("data", []))
    except Exception as e:
        logger.debug("Poll trades for %s failed: %s", wallet[:10], e)
    return []


async def _detect_new_trades(wallet: str, trades: List[Dict]) -> List[Dict]:
    """Filter trades we haven't seen before."""
    last_id = _last_seen_trades.get(wallet)
    new_trades = []

    if not trades:
        return new_trades

    # Trades should be newest-first
    for t in trades:
        tid = t.get("id", "")
        if tid == last_id:
            break
        new_trades.append(t)

    if trades:
        _last_seen_trades[wallet] = trades[0].get("id", "")

    return new_trades


# ────────────── Primary monitor loop ──────────────

async def _ws_monitor():
    """
    Primary monitoring loop.

    Uses HTTP polling as the reliable backbone.  Optionally attempts a
    WebSocket connection for lower-latency detection; if the WS fails it
    silently falls back to polling without spamming logs.
    """
    _ws_available = False
    try:
        import websockets  # noqa: F401
        _ws_available = True
    except ImportError:
        logger.info("websockets package not installed – HTTP polling only")

    ws_backoff = 5          # initial WS retry delay (seconds)
    ws_consecutive_fails = 0
    MAX_WS_FAILS = 5        # after N consecutive failures, stop trying WS

    while True:
        # ── Always run an HTTP poll cycle first ──
        if _watched_wallets:
            await _http_poll_cycle()

        # ── Optionally attempt a WebSocket session ──
        if _ws_available and _watched_wallets and ws_consecutive_fails < MAX_WS_FAILS:
            try:
                await asyncio.wait_for(
                    _ws_session(), timeout=60,
                )
                ws_consecutive_fails = 0
                ws_backoff = 5
            except (asyncio.TimeoutError, asyncio.CancelledError):
                raise  # let cancellation propagate
            except Exception as e:
                ws_consecutive_fails += 1
                # Only log the first failure and when we give up
                if ws_consecutive_fails == 1:
                    logger.info("WebSocket unavailable, relying on HTTP polling: %s", e)
                elif ws_consecutive_fails >= MAX_WS_FAILS:
                    logger.info(
                        "WebSocket failed %d times consecutively – disabling WS, using HTTP polling only",
                        MAX_WS_FAILS,
                    )

        # Wait before next poll cycle (30 s with wallets, 60 s idle)
        interval = 30 if _watched_wallets else 60
        await asyncio.sleep(interval)


async def _ws_session():
    """
    Single WebSocket session.  Returns normally when the connection closes
    so the outer loop can retry or fall back.
    """
    import websockets
    import websockets.exceptions

    async with websockets.connect(
        POLYMARKET_WS_URL,
        ping_interval=25,
        ping_timeout=20,
        close_timeout=5,
        additional_headers={"User-Agent": "polymarket-ai/1.0"},
    ) as ws:
        logger.info("WebSocket connected to %s", POLYMARKET_WS_URL)

        # Subscribe to trade events for each watched wallet
        for wallet in _watched_wallets:
            sub_msg = json.dumps({
                "type": "subscribe",
                "channel": "user",
                "user": wallet,
            })
            await ws.send(sub_msg)

        async for message in ws:
            try:
                data = json.loads(message)
                await _handle_ws_event(data)
            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error("WS event handler error: %s", e)


async def _handle_ws_event(data: Dict):
    """Process a WebSocket trade event."""
    event_type = data.get("type", "")
    if event_type not in ("trade", "fill", "match"):
        return

    wallet = (data.get("user") or data.get("taker") or data.get("maker") or "").lower()
    if wallet not in _watched_wallets:
        return

    logger.info("WS trade event for watched wallet %s", wallet[:10])
    await _process_new_trade(wallet, data)


# ────────────── HTTP polling loop ──────────────

# ────────────── HTTP polling ──────────────


async def _http_poll_cycle():
    """One round of polling for all watched wallets."""
    if not _watched_wallets:
        return

    wallets = list(_watched_wallets)
    started = perf_counter()
    tasks = [_poll_and_process(w) for w in wallets]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = sum(1 for r in results if isinstance(r, Exception))
    elapsed_ms = (perf_counter() - started) * 1000
    logger.info(
        "Trade poll cycle complete: wallets=%d errors=%d elapsed_ms=%.1f",
        len(wallets),
        errors,
        elapsed_ms,
    )


async def _poll_and_process(wallet: str):
    """Poll one wallet and process any new trades."""
    async with _poll_semaphore:
        trades = await _poll_trader_trades(wallet)
        new_trades = await _detect_new_trades(wallet, trades)
        for trade in new_trades:
            await _process_new_trade(wallet, trade)


# ────────────── Trade processing pipeline ──────────────

async def _process_new_trade(wallet: str, trade_data: Dict):
    """
    Process a newly detected trade from a followed trader:
    1. Record in trade_history
    2. Ensure the market exists in the markets table
    3. Trigger copy-trade for each subscribing user
    """
    db = SessionLocal()
    try:
        # Extract trade fields
        market_id = trade_data.get("market") or trade_data.get("condition_id") or trade_data.get("asset_id") or ""
        token_id = trade_data.get("token_id") or trade_data.get("asset_id") or ""
        side = (trade_data.get("side") or "BUY").upper()
        amount = _to_float(trade_data.get("size", 0))
        price = _to_float(trade_data.get("price", 0))
        trade_id_ext = trade_data.get("id", "")
        notional_usdc = _to_float(trade_data.get("usdcSize", 0))
        if notional_usdc <= 0 and amount > 0 and price > 0:
            notional_usdc = amount * price

        if not market_id or amount <= 0:
            return

        # Ensure market record exists
        _ensure_market(db, market_id, trade_data)

        # Record trade_history
        ts = _parse_timestamp(trade_data.get("match_time") or trade_data.get("timestamp"))
        th = TradeHistory(
            market_id=market_id,
            wallet_address=wallet,
            order_type=side.lower(),
            amount=amount,
            price=price,
            notional_usdc=notional_usdc if notional_usdc > 0 else None,
            source_trade_id_ext=str(trade_id_ext) if trade_id_ext else None,
            timestamp=ts or utc_now(),
        )
        db.add(th)
        db.commit()
        db.refresh(th)

        logger.info(
            "Recorded trade_history #%d: %s %s %.2f @ %.4f on %s",
            th.id, wallet[:10], side, amount, price, market_id[:20],
        )

        # Classify opened/closed transitions from running net size and emit
        # notification feed/email events for notification followers.
        event_type, prev_net_size, new_net_size = _update_trader_position_state(
            db=db,
            trader_wallet=wallet,
            token_id=token_id or market_id,
            market_id=market_id,
            side=side,
            size=amount,
        )
        if event_type in ("opened", "closed"):
            await _create_follow_notifications(
                db=db,
                trader_wallet=wallet,
                event_type=event_type,
                market_id=market_id,
                token_id=token_id or market_id,
                side=side,
                size=amount,
                price=price,
                prev_net_size=prev_net_size,
                new_net_size=new_net_size,
                source_trade_history_id=th.id,
            )

        # Find all users following this trader
        followers = (
            db.query(FollowedTrader)
            .filter(
                FollowedTrader.trader_wallet == wallet,
                FollowedTrader.is_active == True,
            )
            .all()
        )

        for ft in followers:
            try:
                await _trigger_copy_trade(
                    db=db,
                    user_id=ft.user_id,
                    trader_wallet=wallet,
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    price=price,
                    trader_amount=notional_usdc if notional_usdc > 0 else amount,
                    trade_history_id=th.id,
                )
            except Exception as e:
                logger.error("Copy-trade trigger failed for user %d: %s", ft.user_id, e)

    except Exception as e:
        logger.error("_process_new_trade error: %s", e)
        db.rollback()
    finally:
        db.close()


async def _trigger_copy_trade(
    db: Session,
    user_id: int,
    trader_wallet: str,
    market_id: str,
    token_id: str,
    side: str,
    price: float,
    trader_amount: float,
    trade_history_id: int,
):
    """
    Optionally run AI analysis, then execute the copy trade.
    """
    from app.services.copy_trade_service import execute_copy_trade

    # Check if the user wants AI analysis before execution
    user_settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    assessment = None

    if user_settings and user_settings.require_ai_approval:
        try:
            assessment = await _run_ai_evaluation(
                db=db,
                user_id=user_id,
                trader_wallet=trader_wallet,
                market_id=market_id,
                side=side,
                price=price,
                amount=trader_amount,
            )
        except Exception as e:
            logger.warning("AI evaluation failed for user %d, proceeding without: %s", user_id, e)

    await execute_copy_trade(
        db=db,
        user_id=user_id,
        trader_wallet=trader_wallet,
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        trader_amount=trader_amount,
        trade_history_id=trade_history_id,
        assessment=assessment,
    )


async def _run_ai_evaluation(
    db: Session,
    user_id: int,
    trader_wallet: str,
    market_id: str,
    side: str,
    price: float,
    amount: float,
) -> Optional[Dict[str, Any]]:
    """
    Call the LLM backend to evaluate whether to copy a trade.
    Uses the user's preferred backend (LLM Chain or CLI Agent).
    """
    try:
        from app.config import AIBackend
        from app.grpc_clients.analysis_client import AnalysisClient
        from app.models.user_settings import AIBackendType
        from app.models.winner import Winner

        # Determine backend preference
        settings_record = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        backend = AIBackend.LLM_CHAIN
        if settings_record and settings_record.ai_backend == AIBackendType.CLI_AGENT.value:
            backend = AIBackend.CLI_AGENT

        # Get trader stats
        winner = db.query(Winner).filter(Winner.wallet_address == trader_wallet).first()
        trader_stats = ""
        if winner:
            trader_stats = (
                f"Win rate: {winner.win_rate}%, Total PnL: ${winner.total_pnl}, "
                f"Trade count: {winner.trade_count}, Markets: {winner.markets_traded}"
            )

        client = AnalysisClient(backend=backend)
        try:
            result = await client.evaluate_copy_trade(
                trader_wallet=trader_wallet,
                trader_stats=trader_stats,
                market_id=market_id,
                trade_side=side,
                trade_size=amount,
                current_price=price,
            )
            return result
        finally:
            await client.close()

    except Exception as e:
        logger.error("AI evaluation error: %s", e)
        return None


def _update_trader_position_state(
    db: Session,
    trader_wallet: str,
    token_id: str,
    market_id: str,
    side: str,
    size: float,
) -> tuple[Optional[str], float, float]:
    """
    Update running net size and classify opened/closed transitions.

    Returns: (event_type, prev_net_size, new_net_size)
    where event_type is one of: opened | closed | None
    """
    normalized_wallet = trader_wallet.lower()
    normalized_token = token_id or market_id
    if not normalized_token:
        return None, 0.0, 0.0

    row = (
        db.query(TraderPositionState)
        .filter(
            TraderPositionState.trader_wallet == normalized_wallet,
            TraderPositionState.token_id == normalized_token,
        )
        .first()
    )
    prev_net = float(row.net_size) if row else 0.0
    delta = size if side.upper() == "BUY" else -size
    new_net = prev_net + delta
    if abs(new_net) <= POSITION_EPSILON:
        new_net = 0.0

    if row:
        row.net_size = new_net
        row.market_id = market_id or row.market_id
        row.updated_at = utc_now()
    else:
        row = TraderPositionState(
            trader_wallet=normalized_wallet,
            token_id=normalized_token,
            market_id=market_id or "",
            net_size=new_net,
        )
        db.add(row)
    db.commit()

    event_type: Optional[str] = None
    if prev_net <= POSITION_EPSILON and new_net > POSITION_EPSILON:
        event_type = "opened"
    elif prev_net > POSITION_EPSILON and new_net <= POSITION_EPSILON:
        event_type = "closed"
    return event_type, prev_net, new_net


def _smtp_ready() -> bool:
    return is_smtp_configured()


async def _create_follow_notifications(
    db: Session,
    trader_wallet: str,
    event_type: str,
    market_id: str,
    token_id: str,
    side: str,
    size: float,
    price: float,
    prev_net_size: float,
    new_net_size: float,
    source_trade_history_id: int,
) -> None:
    """Persist feed events and optionally send immediate SMTP emails."""
    followers = (
        db.query(NotificationFollowedTrader)
        .filter(
            NotificationFollowedTrader.trader_wallet == trader_wallet.lower(),
            NotificationFollowedTrader.is_active == True,
        )
        .all()
    )
    if not followers:
        return

    user_ids = [f.user_id for f in followers]
    users = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(user_ids)).all()
    }
    settings_by_user = {
        s.user_id: s
        for s in db.query(UserSettings).filter(UserSettings.user_id.in_(user_ids)).all()
    }
    smtp_available = _smtp_ready()

    for follower in followers:
        if not (follower.feed_enabled or follower.email_enabled):
            continue

        event = NotificationFeedEvent(
            user_id=follower.user_id,
            trader_wallet=trader_wallet.lower(),
            event_type=event_type,
            market_id=market_id or "",
            token_id=token_id or "",
            side=side.upper(),
            size=size,
            price=price,
            prev_net_size=prev_net_size,
            new_net_size=new_net_size,
            source_trade_history_id=source_trade_history_id,
            email_status="pending" if follower.email_enabled else "skipped",
        )
        db.add(event)
        db.flush()

        if not follower.email_enabled:
            continue

        user = users.get(follower.user_id)
        settings = settings_by_user.get(follower.user_id)
        if not user or not user.email:
            event.email_status = "skipped"
            event.email_error = "User email is not configured."
            continue
        if not settings or not settings.follow_email_notifications_enabled:
            event.email_status = "skipped"
            event.email_error = "Global follow-email notifications are disabled."
            continue
        if not smtp_available:
            event.email_status = "skipped"
            event.email_error = "SMTP is not configured."
            continue

        try:
            await send_follow_event_email(
                to_email=user.email,
                trader_wallet=trader_wallet,
                event_type=event_type,
                market_id=market_id,
                side=side,
                size=size,
                price=price,
            )
            event.email_status = "sent"
            event.email_error = None
            event.emailed_at = utc_now()
        except Exception as e:
            event.email_status = "failed"
            event.email_error = str(e)[:500]

    db.commit()


# ────────────── Helpers ──────────────

def _ensure_market(db: Session, market_id: str, trade_data: Dict):
    """Create a market record if it doesn't exist yet."""
    existing = db.query(Market).filter(Market.id == market_id).first()
    if existing:
        return
    try:
        market = Market(
            id=market_id,
            question=trade_data.get("question") or trade_data.get("title") or market_id[:80],
            status="active",
        )
        db.add(market)
        db.commit()
    except Exception:
        db.rollback()


def _to_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _parse_timestamp(val) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromtimestamp(int(val), tz=timezone.utc)
    except (ValueError, OSError, TypeError):
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            return None


# ────────────── Lifecycle ──────────────

async def start_trade_monitor():
    """Start the trade monitor as a background asyncio task."""
    global _monitor_task, _poll_http_client

    # Initial watch-list load
    db = SessionLocal()
    try:
        refresh_watched_wallets(db)
    finally:
        db.close()

    if _poll_http_client is None:
        limits = httpx.Limits(
            max_connections=_POLL_HTTP_MAX_CONNECTIONS,
            max_keepalive_connections=_POLL_HTTP_KEEPALIVE,
        )
        _poll_http_client = httpx.AsyncClient(timeout=15.0, limits=limits)

    _monitor_task = asyncio.create_task(_ws_monitor())
    logger.info("Trade monitor started (watching %d wallets)", len(_watched_wallets))


async def stop_trade_monitor():
    """Cancel the monitor task."""
    global _monitor_task, _poll_http_client
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
    _monitor_task = None
    if _poll_http_client is not None:
        await _poll_http_client.aclose()
        _poll_http_client = None
    logger.info("Trade monitor stopped")


def add_watched_wallet(wallet: str):
    """Add a wallet to the live watch list."""
    _watched_wallets.add(wallet.lower())


def remove_watched_wallet(wallet: str):
    """Remove a wallet from the live watch list."""
    _watched_wallets.discard(wallet.lower())
