"""
Trade monitor – watches followed traders' activity via WebSocket + HTTP polling.

Uses a dual strategy:
  1. WebSocket: Connect to Polymarket's CLOB WebSocket for real-time trade events.
  2. HTTP polling: Fallback that polls CLOB /data/trades for each watched wallet.

When a new trade is detected the monitor:
  - Records it in trade_history
  - Calls the LLM analysis pipeline (if enabled)
  - Triggers copy-trade execution for each subscribing user

Both channels feed the same dedup + cursor logic so a trade
is processed exactly once per wallet.  The dedup set is
shared through the cache backend (Redis when configured)
so the WS and HTTP paths — and every worker and restart —
agree on what was already processed.

Also owns the fill-tracking reconciliation jobs:

- Every 60s, pending orders older than 30s are resolved
  against the CLOB (``GET /data/trades?order_id=``) so a
  UserTrade flips pending → executed only on an observed
  fill, with ``executed_at`` set from the fill time.
- Every 15 minutes, local-pending orders older than
  ``GTC_TTL_SECONDS`` are reconciled against the exchange's
  open-order book and cancelled when orphaned.
"""

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.followed_trader import FollowedTrader
from app.models.market import Market
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.notification_followed_trader import NotificationFollowedTrader
from app.models.trade_history import TradeHistory
from app.models.trader_position_state import TraderPositionState
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.security.credential_store import (
    CredentialStoreError,
    load_wallet_credentials,
)
from app.services.email_service import is_smtp_configured, send_follow_event_email
from app.services.execution_analytics import (
    compute_latency_ms,
    compute_slippage_bps,
)
from app.services.polymarket_service import POLYMARKET_CLOB_API
from app.services.pre_trade_gate import (
    build_clob_client,
    clob_trades_for_order,
)
from app.utils.database import SessionLocal
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

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

# Fill reconciliation: how often to run, and how old a
# pending order must be before the CLOB is asked about it
# (fresh submissions get a grace window to land).
FILL_RECON_INTERVAL_SECONDS = 60
FILL_RECON_PENDING_AGE_SECONDS = 30

# Orphaned-GTC reconciliation: how often to run.  Orders
# pending longer than GTC_TTL_SECONDS are cancelled on the
# exchange and marked cancelled locally.
GTC_RECON_INTERVAL_SECONDS = 900  # 15 minutes

# Defensive imports for cross-plan integration.  Plan 02
# owns the cross-worker advisory lock; plan 09 owns alert
# emission.  Both are optional here — when a module is
# absent the jobs fall back to a process-local guard and
# skip alerting.
try:
    from app.utils.scheduler_lock import (  # plan 02
        acquire_scheduler_lock,
        scheduler_heartbeat,
    )

    _SCHEDULER_LOCK_AVAILABLE = True
except ImportError:
    acquire_scheduler_lock = None  # type: ignore[assignment]
    scheduler_heartbeat = None  # type: ignore[assignment]
    _SCHEDULER_LOCK_AVAILABLE = False

try:
    from app.services.alert_service import dispatch as _alert_dispatch  # plan 09
except ImportError:
    _alert_dispatch = None  # type: ignore[assignment]

# In-memory state
_watched_wallets: set[str] = set()
_last_seen_trades: dict[str, str] = {}  # wallet → last trade id
# Per-wallet LRU mirror of processed trade ids.  A WS event
# and the HTTP poll can deliver the same trade twice, and
# trade_history has no unique constraint on source_trade_id_ext
# to catch the replay, so the authoritative dedup set lives in
# the shared cache (see _already_processed); this mirror only
# bounds the local lookups.
_processed_trade_ids: dict[str, OrderedDict[str, None]] = {}
_PROCESSED_IDS_MAX = 500
_PROCESSED_IDS_TTL_SECONDS = 86400  # 24h
_monitor_task: asyncio.Task | None = None
_fill_recon_task: asyncio.Task | None = None
_gtc_recon_task: asyncio.Task | None = None
_poll_http_client: httpx.AsyncClient | None = None
_poll_semaphore: asyncio.Semaphore = asyncio.Semaphore(_POLL_MAX_CONCURRENCY)
POSITION_EPSILON = 1e-9

# Shared (Redis-backed, 24h TTL) dedup set.  Lazily created
# so importing this module never fails when the cache backend
# is misconfigured.
_processed_ids_cache = None


# ────────────── Wallet watch list management ──────────────


def refresh_watched_wallets(db: Session):
    """Rebuild the set of wallets being followed by any active user."""
    global _watched_wallets
    copy_rows = (
        db.query(FollowedTrader.trader_wallet).filter(FollowedTrader.is_active).distinct().all()
    )
    notif_rows = (
        db.query(NotificationFollowedTrader.trader_wallet)
        .filter(NotificationFollowedTrader.is_active)
        .distinct()
        .all()
    )
    _watched_wallets = {r[0].lower() for r in (copy_rows + notif_rows) if r and r[0]}
    logger.info("Watch list refreshed: %d unique wallets", len(_watched_wallets))


# ────────────── HTTP polling (reliable fallback) ──────────────


async def _poll_trader_trades(wallet: str) -> list[dict]:
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


async def seed_wallet_cursor(wallet: str) -> None:
    """Seed the per-wallet cursor with the wallet's newest trade id.

    Called when a wallet starts being watched (follow) and
    before the WS subscribe, so the monitor's first poll
    does not replay the wallet's trade history as "new"
    trades (which would fire copy orders for trades the
    follower never saw live).
    """
    normalized = wallet.lower()
    if normalized in _last_seen_trades:
        return
    try:
        client = _poll_http_client
        params = {"user": normalized, "limit": 1}
        if client is None:
            async with httpx.AsyncClient(timeout=15.0) as temp_client:
                resp = await temp_client.get(
                    f"{POLYMARKET_CLOB_API}/data/trades",
                    params=params,
                )
        else:
            resp = await client.get(
                f"{POLYMARKET_CLOB_API}/data/trades",
                params=params,
            )
        if resp.status_code == 200:
            data = resp.json()
            trades = data if isinstance(data, list) else data.get("trades", data.get("data", []))
            if trades:
                _last_seen_trades[normalized] = trades[0].get("id", "")
                logger.info(
                    "Seeded trade cursor for %s at %s",
                    normalized[:10],
                    _last_seen_trades[normalized],
                )
    except Exception as e:
        logger.warning("Could not seed trade cursor for %s: %s", normalized[:10], e)


async def _detect_new_trades(wallet: str, trades: list[dict]) -> list[dict]:
    """Filter trades we haven't seen before."""
    last_id = _last_seen_trades.get(wallet)
    new_trades = []

    if not trades:
        return new_trades

    if last_id is None:
        # First observation: seed the cursor with the newest trade id
        # and skip history. Without this the first poll replays up to
        # `limit` historical trades as if they were new, firing copy
        # orders for trades the follower never saw live.
        _last_seen_trades[wallet] = trades[0].get("id", "")
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


def _trade_dedup_key(trade_data: dict) -> str | None:
    """Build a dedup key for a trade event, or None when unidentifiable.

    Prefers the exchange trade id — ``id`` on HTTP poll rows,
    ``trade_id`` on WS user-channel events.  Events carrying neither
    fall back to a content fingerprint so identical deliveries are
    still recognised as duplicates.
    """
    trade_id = trade_data.get("id") or trade_data.get("trade_id")
    if trade_id:
        return str(trade_id)
    market_id = (
        trade_data.get("market") or trade_data.get("condition_id") or trade_data.get("asset_id")
    )
    side = trade_data.get("side")
    size = trade_data.get("size")
    price = trade_data.get("price")
    match_time = trade_data.get("match_time") or trade_data.get("timestamp")
    if market_id and side and size is not None and price is not None:
        return f"fp:{market_id}:{side}:{size}:{price}:{match_time or ''}"
    return None


def _get_processed_ids_cache():
    """Lazily create the shared dedup cache (Redis when configured)."""
    global _processed_ids_cache
    if _processed_ids_cache is None:
        from app.utils.cache import get_cache

        _processed_ids_cache = get_cache("trade_dedup")
    return _processed_ids_cache


def _shared_already_processed(wallet: str, key: str) -> bool:
    """Check the shared dedup set (Redis when configured)."""
    try:
        return _get_processed_ids_cache().get(f"{wallet}:{key}") is not None
    except Exception as e:
        logger.warning("Shared trade-dedup read failed: %s", e)
        return False


def _shared_mark_processed(wallet: str, key: str) -> None:
    """Record a processed trade in the shared dedup set (24h TTL)."""
    try:
        _get_processed_ids_cache().set(
            f"{wallet}:{key}",
            True,
            ttl_seconds=_PROCESSED_IDS_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning("Shared trade-dedup write failed: %s", e)


def _already_processed(wallet: str, key: str) -> bool:
    """Return True when `key` was already processed for `wallet`.

    Checks the process-local mirror first, then the shared
    (Redis-backed) set so WS and HTTP deliveries — across
    workers and restarts — dedupe against each other.
    Records the key on first sight.  The local mirror stays
    bounded: the oldest entries are evicted once it grows
    past ``_PROCESSED_IDS_MAX``.
    """
    seen = _processed_trade_ids.setdefault(wallet, OrderedDict())
    if key in seen:
        seen.move_to_end(key)
        return True
    if _shared_already_processed(wallet, key):
        seen[key] = None
        while len(seen) > _PROCESSED_IDS_MAX:
            seen.popitem(last=False)
        return True
    seen[key] = None
    while len(seen) > _PROCESSED_IDS_MAX:
        seen.popitem(last=False)
    _shared_mark_processed(wallet, key)
    return False


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

    ws_consecutive_fails = 0
    MAX_WS_FAILS = 5  # after N consecutive failures, stop trying WS

    while True:
        # ── Always run an HTTP poll cycle first ──
        if _watched_wallets:
            await _http_poll_cycle()

        # ── Optionally attempt a WebSocket session ──
        if _ws_available and _watched_wallets and ws_consecutive_fails < MAX_WS_FAILS:
            try:
                await asyncio.wait_for(
                    _ws_session(),
                    timeout=60,
                )
                ws_consecutive_fails = 0
            except (TimeoutError, asyncio.CancelledError):
                raise  # let cancellation propagate
            except Exception as e:
                ws_consecutive_fails += 1
                # Only log the first failure and when we give up
                if ws_consecutive_fails == 1:
                    logger.info("WebSocket unavailable, relying on HTTP polling: %s", e)
                elif ws_consecutive_fails >= MAX_WS_FAILS:
                    logger.info(
                        "WebSocket failed %d times consecutively – "
                        "disabling WS, using HTTP polling only",
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

        # Seed the per-wallet cursor before subscribing so
        # the wallet's trade history is not replayed as new
        # trades (and copied) on the first WS delivery.
        for wallet in _watched_wallets:
            await seed_wallet_cursor(wallet)

        # Subscribe to trade events for each watched wallet
        for wallet in _watched_wallets:
            sub_msg = json.dumps(
                {
                    "type": "subscribe",
                    "channel": "user",
                    "user": wallet,
                }
            )
            await ws.send(sub_msg)

        async for message in ws:
            try:
                data = json.loads(message)
                await _handle_ws_event(data)
            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error("WS event handler error: %s", e)


async def _handle_ws_event(data: dict):
    """Process a WebSocket trade event."""
    event_type = data.get("type", "")
    if event_type not in ("trade", "fill", "match"):
        return

    # Fill tracking: a user-channel event carrying an
    # order hash resolves any local pending order with
    # that hash.  This runs before the watched-wallet
    # filter because the event's wallet is the wallet
    # that traded — the follower whose copy order filled
    # is not necessarily a watched wallet.  The 60s
    # reconciliation job is the backstop for events
    # that never arrive.
    order_hash = _event_order_hash(data)
    if order_hash:
        db = SessionLocal()
        try:
            _mark_pending_trades_filled(
                db,
                order_hash,
                filled_at=_parse_timestamp(data.get("match_time") or data.get("timestamp")),
                fill_price=_to_float(data.get("price"), None),
                fill_size=_to_float(data.get("size"), None),
                fill_fee=_to_float(data.get("fee"), None),
            )
        finally:
            db.close()

    wallet = (data.get("user") or data.get("taker") or data.get("maker") or "").lower()
    if wallet not in _watched_wallets:
        return

    logger.info("WS trade event for watched wallet %s", wallet[:10])
    await _process_new_trade(wallet, data)


def _event_order_hash(data: dict) -> str | None:
    """Extract the order hash from a WS user-channel event."""
    for field in ("order_hash", "order_id", "hash", "orderID"):
        value = data.get(field)
        if value:
            return str(value)
    return None


def _mark_pending_trades_filled(
    db: Session,
    order_hash: str,
    filled_at: datetime | None = None,
    fill_price: float | None = None,
    fill_size: float | None = None,
    fill_fee: float | None = None,
) -> int:
    """Mark pending UserTrades with this order hash as executed.

    ``executed_at`` is set from the observed fill time —
    never from submission time — so PnL and loss limits
    key off real fills.  When the fill event carries them,
    the observed fill price/size/fee are recorded along with
    the derived slippage (vs the expected price captured at
    submission) and submission→fill latency (plan 03).
    """
    if not order_hash:
        return 0
    pending = (
        db.query(UserTrade)
        .filter(
            UserTrade.order_hash == order_hash,
            UserTrade.status == "pending",
        )
        .all()
    )
    if not pending:
        return 0
    filled_at = filled_at or utc_now()
    for row in pending:
        row.status = "executed"
        row.executed_at = filled_at
        if fill_price is not None:
            row.filled_price = fill_price
        if fill_size is not None:
            row.filled_size = fill_size
        # Missing fee data is treated as 0 (and counted by the
        # analytics summary's data-quality block).
        row.fee_paid = float(fill_fee) if fill_fee is not None else 0.0
        if row.expected_price is not None and row.filled_price is not None:
            row.slippage_bps = compute_slippage_bps(row.expected_price, row.filled_price)
        row.latency_ms = compute_latency_ms(row.created_at, filled_at)
    db.commit()
    logger.info(
        "Marked %d pending trade(s) executed for order %s",
        len(pending),
        order_hash[:16],
    )
    return len(pending)


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


async def _process_new_trade(wallet: str, trade_data: dict):
    """
    Process a newly detected trade from a followed trader:
    1. Record in trade_history
    2. Ensure the market exists in the markets table
    3. Trigger copy-trade for each subscribing user
    """
    # WS events and the HTTP poll can deliver the same trade;
    # drop duplicates before any DB work.
    dedup_key = _trade_dedup_key(trade_data)
    if dedup_key and _already_processed(wallet, dedup_key):
        logger.debug("Skipping duplicate trade event %s", dedup_key[:40])
        return

    db = SessionLocal()
    try:
        # Extract trade fields
        market_id = (
            trade_data.get("market")
            or trade_data.get("condition_id")
            or trade_data.get("asset_id")
            or ""
        )
        token_id = trade_data.get("token_id") or trade_data.get("asset_id") or ""
        side = (trade_data.get("side") or "BUY").upper()
        amount = _to_float(trade_data.get("size", 0))
        price = _to_float(trade_data.get("price", 0))
        trade_id_ext = trade_data.get("id") or trade_data.get("trade_id") or ""
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
            th.id,
            wallet[:10],
            side,
            amount,
            price,
            market_id[:20],
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
                FollowedTrader.is_active,
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
        except Exception:
            logger.exception("AI evaluation raised for user %d", user_id)
            assessment = None

        # Fail closed. `_run_ai_evaluation` returns None both on error and when
        # the model produced no usable assessment, so an absent assessment means
        # "not approved". A gate that opens when the system behind it is broken
        # is not a gate: skipping a copy trade is recoverable, executing an
        # unapproved one against the user's capital is not.
        if not assessment:
            logger.error(
                "AI approval required but unavailable for user %d "
                "(market %s); skipping copy trade %s",
                user_id,
                market_id,
                trade_history_id,
            )
            return

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
) -> dict[str, Any] | None:
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
            return await client.evaluate_copy_trade(
                trader_wallet=trader_wallet,
                trader_stats=trader_stats,
                market_id=market_id,
                trade_side=side,
                trade_size=amount,
                current_price=price,
            )
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
) -> tuple[str | None, float, float]:
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

    event_type: str | None = None
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
            NotificationFollowedTrader.is_active,
        )
        .all()
    )
    if not followers:
        return

    user_ids = [f.user_id for f in followers]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
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


# ────────────── Fill reconciliation ──────────────


async def _emit_alert(user_id: int, event_type: str, message: str) -> None:
    """Emit an alert event (plan 09) when the service is available."""
    if _alert_dispatch is None:
        logger.debug(
            "Alert service unavailable; skipping %s alert for user %s",
            event_type,
            user_id,
        )
        return
    try:
        await _alert_dispatch(event_type, user_id, {"message": message})
    except Exception:
        logger.warning(
            "Alert emission failed for user %s",
            user_id,
            exc_info=True,
        )


def _gtc_ttl_seconds() -> int:
    try:
        return max(1, int(get_settings().gtc_ttl_seconds))
    except Exception:
        return 86400


def _acquire_job_lock(job_name: str) -> bool:
    """Acquire the cross-worker advisory lock for a reconciliation job.

    Uses plan 02's scheduler_lock when available; otherwise the
    loop task itself is the process-local guard (exactly one
    loop runs per process).
    """
    if acquire_scheduler_lock is not None:
        acquired = acquire_scheduler_lock(job_name)
        if not acquired:
            logger.warning(
                "Another worker owns the '%s' lock; skipping this job",
                job_name,
            )
        return acquired
    return True


def _job_heartbeat(job_name: str) -> None:
    """Refresh the dead-man's-switch heartbeat (plan 02)."""
    if scheduler_heartbeat is not None:
        scheduler_heartbeat(job_name)


async def _fill_reconciliation_loop():
    """Every 60s, resolve pending orders older than 30s against the CLOB."""
    job_name = "fill_reconciliation"
    if not _acquire_job_lock(job_name):
        return
    while True:
        await asyncio.sleep(FILL_RECON_INTERVAL_SECONDS)
        _job_heartbeat(job_name)
        try:
            await _reconcile_pending_fills()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending-fill reconciliation failed")


async def _reconcile_pending_fills() -> None:
    """Query the CLOB for fills of pending orders older than the grace window."""
    cutoff = utc_now() - timedelta(seconds=FILL_RECON_PENDING_AGE_SECONDS)
    db = SessionLocal()
    try:
        pending = (
            db.query(UserTrade)
            .filter(
                UserTrade.status == "pending",
                UserTrade.created_at < cutoff,
                UserTrade.order_hash.isnot(None),
            )
            .all()
        )
        if not pending:
            return
        by_user: dict[int, list[UserTrade]] = {}
        for row in pending:
            by_user.setdefault(row.user_id, []).append(row)
        for user_id, rows in by_user.items():
            await _reconcile_fills_for_user(db, user_id, rows)
    finally:
        db.close()


async def _reconcile_fills_for_user(
    db: Session,
    user_id: int,
    rows: list[UserTrade],
) -> None:
    """Check one user's pending orders against the CLOB trade feed."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return
    try:
        stored = load_wallet_credentials(user.wallet_address)
    except CredentialStoreError:
        logger.warning(
            "Credential store unavailable; skipping fill reconciliation for user %s",
            user_id,
        )
        return
    if not stored:
        return
    pk = stored["private_key"]
    creds = stored.get("clob_creds")
    proxy_address = _resolve_proxy_address(pk)
    client = build_clob_client(pk, creds, proxy_address=proxy_address)
    for row in rows:
        try:
            trades = await asyncio.to_thread(clob_trades_for_order, client, row.order_hash)
        except Exception as e:
            logger.warning(
                "Fill lookup failed for order %s: %s",
                (row.order_hash or "")[:16],
                e,
            )
            continue
        if not trades:
            # Still resting (or the exchange has no record
            # yet) — leave pending for the next cycle.
            continue
        fill = trades[0] or {}
        filled_at = _parse_timestamp(fill.get("match_time"))
        _mark_pending_trades_filled(
            db,
            row.order_hash,
            filled_at=filled_at,
            fill_price=_to_float(fill.get("price"), None),
            fill_size=_to_float(fill.get("size"), None),
            fill_fee=_to_float(fill.get("fee"), None),
        )


def _resolve_proxy_address(private_key: str) -> str | None:
    """Resolve the wallet's POLY proxy address, if any."""
    try:
        from py_clob_client.signer import Signer

        from app.services.copy_trade_service import _get_poly_proxy_wallet_address

        return _get_poly_proxy_wallet_address(Signer(private_key, 137).address())
    except Exception as e:
        logger.debug("Proxy resolution failed: %s", e)
        return None


# ────────────── Orphaned-GTC reconciliation ──────────────


async def _gtc_reconciliation_loop():
    """Every 15 minutes, cancel local-pending orders older than GTC_TTL.

    Ticks every 60s so the dead-man's-switch heartbeat
    (plan 02) stays fresh; the reconciliation itself only
    runs once per GTC_RECON_INTERVAL_SECONDS.
    """
    job_name = "gtc_reconciliation"
    if not _acquire_job_lock(job_name):
        return
    last_run = 0.0
    while True:
        await asyncio.sleep(60)
        _job_heartbeat(job_name)
        now = perf_counter()
        if now - last_run < GTC_RECON_INTERVAL_SECONDS:
            continue
        last_run = now
        try:
            await _reconcile_orphaned_gtc_orders()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Orphaned-GTC reconciliation failed")


async def _reconcile_orphaned_gtc_orders() -> None:
    """Cancel local-pending orders that outlived the GTC TTL.

    An order pending longer than ``GTC_TTL_SECONDS`` is
    orphaned: either it still rests on the exchange (cancel
    it there) or the exchange already dropped it (mark it
    cancelled locally, unless it actually filled).  Local
    state and exchange state are reconciled either way, and
    a mismatch raises an alert event.
    """
    gtc_ttl = _gtc_ttl_seconds()
    cutoff = utc_now() - timedelta(seconds=gtc_ttl)
    db = SessionLocal()
    try:
        stale = (
            db.query(UserTrade)
            .filter(
                UserTrade.status == "pending",
                UserTrade.created_at < cutoff,
                UserTrade.order_hash.isnot(None),
            )
            .all()
        )
        if not stale:
            return
        by_user: dict[int, list[UserTrade]] = {}
        for row in stale:
            by_user.setdefault(row.user_id, []).append(row)
        for user_id, rows in by_user.items():
            await _reconcile_gtc_for_user(db, user_id, rows, gtc_ttl)
    finally:
        db.close()


async def _reconcile_gtc_for_user(
    db: Session,
    user_id: int,
    rows: list[UserTrade],
    gtc_ttl: int,
) -> None:
    """Reconcile one user's stale pending orders against the exchange."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return
    try:
        stored = load_wallet_credentials(user.wallet_address)
    except CredentialStoreError:
        logger.warning(
            "Credential store unavailable; skipping GTC reconciliation for user %s",
            user_id,
        )
        return
    if not stored:
        return
    pk = stored["private_key"]
    creds = stored.get("clob_creds")
    proxy_address = _resolve_proxy_address(pk)
    client = build_clob_client(pk, creds, proxy_address=proxy_address)

    try:
        open_orders = await asyncio.to_thread(client.get_orders)
    except Exception as e:
        logger.warning(
            "Open-order lookup failed for user %s; skipping GTC pass: %s",
            user_id,
            e,
        )
        return
    open_hashes = {
        str(o.get("id") or o.get("order_id") or o.get("orderID") or "") for o in (open_orders or [])
    }

    for row in rows:
        order_hash = row.order_hash
        if order_hash in open_hashes:
            # Still resting on the exchange past the GTC
            # TTL — cancel it there and locally.
            try:
                await asyncio.to_thread(client.cancel, order_hash)
                row.status = "cancelled"
                db.commit()
                await _emit_alert(
                    user_id,
                    "gtc_order_cancelled",
                    f"Cancelled orphaned GTC order {order_hash[:16]}… (pending > {gtc_ttl}s)",
                )
            except Exception as e:
                logger.warning(
                    "Failed to cancel orphaned order %s: %s",
                    order_hash[:16],
                    e,
                )
            continue

        # Gone from the open-order book: either it filled
        # or the exchange dropped it.  Check the trade
        # feed before deciding so a fill is never
        # overwritten with "cancelled".
        try:
            trades = await asyncio.to_thread(clob_trades_for_order, client, order_hash)
        except Exception as e:
            logger.warning(
                "Trade lookup failed for orphaned order %s: %s",
                order_hash[:16],
                e,
            )
            continue
        if trades:
            fill = trades[0] or {}
            filled_at = _parse_timestamp(fill.get("match_time"))
            _mark_pending_trades_filled(
                db,
                order_hash,
                filled_at=filled_at,
                fill_price=_to_float(fill.get("price"), None),
                fill_size=_to_float(fill.get("size"), None),
                fill_fee=_to_float(fill.get("fee"), None),
            )
            await _emit_alert(
                user_id,
                "gtc_order_filled",
                f"Orphaned GTC order {order_hash[:16]}… filled (pending > {gtc_ttl}s)",
            )
        else:
            row.status = "cancelled"
            db.commit()
            await _emit_alert(
                user_id,
                "gtc_order_cancelled",
                f"Cancelled orphaned GTC order {order_hash[:16]}… "
                f"(no longer open on exchange, pending > {gtc_ttl}s)",
            )


# ────────────── Helpers ──────────────


def _ensure_market(db: Session, market_id: str, trade_data: dict):
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


def _parse_timestamp(val) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromtimestamp(int(val), tz=UTC)
    except (ValueError, OSError, TypeError):
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            return None


# ────────────── Lifecycle ──────────────


async def start_trade_monitor():
    """Start the trade monitor as a background asyncio task."""
    global _monitor_task, _poll_http_client, _fill_recon_task, _gtc_recon_task

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
    _fill_recon_task = asyncio.create_task(_fill_reconciliation_loop())
    _gtc_recon_task = asyncio.create_task(_gtc_reconciliation_loop())
    logger.info("Trade monitor started (watching %d wallets)", len(_watched_wallets))


async def stop_trade_monitor():
    """Cancel the monitor task."""
    global _monitor_task, _poll_http_client, _fill_recon_task, _gtc_recon_task
    for task_ref in ("_monitor_task", "_fill_recon_task", "_gtc_recon_task"):
        task = globals()[task_ref]
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        globals()[task_ref] = None
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
