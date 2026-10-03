"""Portfolio API routes - balance, positions, and portfolio summary."""

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from py_clob_client.clob_types import BookParams
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.config import get_settings
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.services.clob_ws_manager import (
    get_clob_ws_manager,
    is_stale_tick,
)
from app.services.polymarket_service import get_polymarket_service
from app.utils.database import get_db
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# --- Response Schemas ---


class WalletBalance(BaseModel):
    """Wallet balance on Polygon."""

    wallet_address: str
    usdc_balance: float = 0.0
    matic_balance: float = 0.0
    chain: str = "polygon"
    error: str | None = None


class Position(BaseModel):
    """A single Polymarket position."""

    market: str | None = None
    title: str | None = None
    outcome: str | None = None
    size: float = 0.0
    avg_price: float = 0.0
    current_price: float = 0.0
    pnl: float = 0.0
    market_slug: str | None = None


class PortfolioSummary(BaseModel):
    """Complete portfolio summary."""

    wallet_address: str
    usdc_balance: float = 0.0
    matic_balance: float = 0.0
    active_positions: int = 0
    total_positions: int = 0
    total_invested: float = 0.0
    total_current_value: float = 0.0
    total_pnl: float = 0.0
    pnl_percentage: float = 0.0
    win_rate: float = 0.0
    wins_positions_history: int = 0
    total_positions_history: int = 0
    wins_positions: int = 0
    resolved_trades: int = 0
    positions: list = []


class MarketSummary(BaseModel):
    """Active market summary."""

    id: str | None = None
    question: str | None = None
    volume_24hr: float | None = None
    liquidity: float | None = None
    end_date: str | None = None
    outcomes: list | None = None


# --- Endpoints ---


@router.get("/balance", response_model=WalletBalance)
async def get_balance(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get USDC and MATIC balance for the authenticated user's wallet."""
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    # Retrieve stored private-key / CLOB creds (if user logged in with PK)
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    balance = await service.get_wallet_balance(wallet_address, private_key=pk, clob_creds=creds)
    return WalletBalance(**balance)


@router.get("/positions")
async def get_positions(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get all Polymarket positions for the authenticated user."""
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    positions = await service.get_positions(wallet_address, private_key=pk, clob_creds=creds)
    return {"wallet_address": wallet_address, "positions": positions, "count": len(positions)}


@router.get("/summary", response_model=PortfolioSummary)
async def get_portfolio_summary(
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    Get complete portfolio summary including balance, positions, P&L, and win rate.
    This is the main endpoint used by the Dashboard.
    """
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    # Retrieve stored private-key / CLOB creds
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    summary = await service.get_portfolio_summary(wallet_address, private_key=pk, clob_creds=creds)
    return PortfolioSummary(**summary)


@router.get("/markets")
async def get_active_markets(
    limit: int = 10,
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get trending active markets from Polymarket."""
    service = get_polymarket_service()
    markets = await service.get_active_markets(limit=limit)
    return {"markets": markets, "count": len(markets)}


@router.get("/markets/newest")
async def get_newest_markets(
    limit: int = 60,
):
    """Get the newest/most recently created markets from Polymarket (public)."""
    service = get_polymarket_service()
    markets = await service.get_newest_markets(limit=limit)
    return {"markets": markets, "count": len(markets)}


@router.get("/markets/combined")
async def get_combined_markets(
    limit: int = 60,
    offset: int = 0,
):
    """
    Return the same active-market universe used by the Markets tab,
    with offset pagination for the Opportunities view.
    """
    service = get_polymarket_service()
    result = await service.search_all_markets(
        query="",
        tag="",
        limit=limit,
        offset=offset,
        sort="volume24hr",
    )
    page = [{**m, "_source": "all_markets"} for m in result.get("markets", [])]
    total = int(result.get("total", 0) or 0)
    result_offset = int(result.get("offset", offset) or 0)
    has_more = bool(result.get("has_more", False))
    return {
        "markets": page,
        "count": len(page),
        "total": total,
        "offset": result_offset,
        "has_more": has_more,
    }


@router.get("/markets/prices")
async def get_market_prices(
    limit: int = 20,
):
    """
    Lightweight endpoint that returns only the fields needed for a price
    refresh: id, outcomePrices, volume24hr, liquidity.

    The frontend calls this every ~15 s so we keep the response small.
    """
    service = get_polymarket_service()
    markets = await service.get_active_markets(limit=limit)
    slim = []
    for m in markets:
        slim.append(
            {
                "id": m.get("condition_id") or m.get("id") or m.get("question", ""),
                "question": m.get("question", ""),
                "outcomePrices": m.get("outcomePrices"),
                "bestAsk": m.get("bestAsk"),
                "bestBid": m.get("bestBid"),
                "lastTradePrice": m.get("lastTradePrice"),
                "volume24hr": m.get("volume24hr"),
                "liquidity": m.get("liquidity"),
                "slug": m.get("slug"),
            }
        )
    return {"markets": slim, "ts": utc_now().isoformat()}


async def _fetch_token_prices(
    wallet_address: str,
    extra_token_ids: list[str] | None = None,
) -> dict[str, float]:
    """
    Best-effort price snapshot for a wallet's open positions plus any
    extra token ids.  Last-trade prices are preferred, with midpoints
    filling the gaps so a partial failure still returns usable prices.
    """
    service = get_polymarket_service()

    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    asset_ids: list[str] = []
    if pk:
        with suppress(Exception):
            positions = await service.get_positions(
                wallet_address, private_key=pk, clob_creds=creds
            )
            asset_ids = [p["asset_id"] for p in positions if p.get("asset_id")]

    token_ids = list(dict.fromkeys(asset_ids + [str(t) for t in (extra_token_ids or []) if t]))
    if not token_ids:
        return {}

    prices: dict[str, float] = {}
    clob = None
    if pk:
        with suppress(Exception):
            clob = service._get_clob_client(pk, creds, signature_type=1)
    if clob is None:
        return prices

    with suppress(Exception):
        prices_resp = clob.get_last_trades_prices(
            [BookParams(token_id=token_id) for token_id in token_ids]
        )
        for entry in prices_resp:
            token_id = entry.get("token_id") or entry.get("asset_id")
            price = service._to_float(entry.get("price"), 0.0)
            if token_id and price > 0:
                prices[token_id] = price

    missing = [token_id for token_id in token_ids if token_id not in prices]
    if missing:
        with suppress(Exception):
            mid_resp = clob.get_midpoints([BookParams(token_id=t) for t in missing])
            for entry in mid_resp:
                token_id = entry.get("token_id") or entry.get("asset_id")
                mid = service._to_float(entry.get("mid"), 0.0)
                if token_id and mid > 0:
                    prices[token_id] = mid

    return prices


@router.get("/positions/prices")
async def get_position_prices(
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    Lightweight endpoint returning current prices for the user's open positions.
    Polled every ~15s by the Dashboard as the fallback for the real-time
    price stream.
    """
    wallet_address = current_user["wallet_address"]
    try:
        prices = await _fetch_token_prices(wallet_address)
    except Exception as e:
        return {"prices": {}, "error": str(e), "ts": utc_now().isoformat()}
    return {"prices": prices, "ts": utc_now().isoformat()}


# ── Real-time price stream (SSE) ──────────────────────────

PRICE_STREAM_COALESCE_SECONDS = 0.25
PRICE_STREAM_POLL_INTERVAL_SECONDS = 15.0
PRICE_STREAM_HEARTBEAT_SECONDS = 15.0
MAX_STREAM_TOKEN_IDS = 200


class SseClientTracker:
    """Caps concurrent SSE clients per user (scale guard)."""

    def __init__(self, max_per_user: int = 5):
        self._max_per_user = max_per_user
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: str) -> bool:
        async with self._lock:
            if self._counts.get(user_id, 0) >= self._max_per_user:
                return False
            self._counts[user_id] = self._counts.get(user_id, 0) + 1
            return True

    async def release(self, user_id: str) -> None:
        async with self._lock:
            self._counts[user_id] = max(0, self._counts.get(user_id, 0) - 1)


_sse_clients = SseClientTracker(max_per_user=get_settings().price_stream_max_sse_clients_per_user)


def _sse_frame(tick: dict) -> str:
    return f"data: {json.dumps(tick)}\n\n"


async def _position_token_ids(wallet_address: str) -> list[str]:
    """Token ids of the user's open positions (server-side resolution)."""
    service = get_polymarket_service()
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None
    if not pk:
        return []
    try:
        positions = await service.get_positions(wallet_address, private_key=pk, clob_creds=creds)
        return [p["asset_id"] for p in positions if p.get("asset_id")]
    except Exception:
        return []


async def _price_stream_frames(
    token_ids: list[str],
    wallet_address: str,
    manager=None,
    coalesce_seconds: float = PRICE_STREAM_COALESCE_SECONDS,
    poll_interval_seconds: float = PRICE_STREAM_POLL_INTERVAL_SECONDS,
    heartbeat_seconds: float = PRICE_STREAM_HEARTBEAT_SECONDS,
):
    """
    Async generator of SSE frames for the price stream.

    While the CLOB WebSocket is connected, ticks are coalesced per
    token_id at `coalesce_seconds` and emitted as
    `{"token_id", "price", "ts"}`.  When the WebSocket is down, a
    poll snapshot is emitted on `poll_interval_seconds` instead
    (same shape), matching the previous polling behaviour.
    """
    if manager is None:
        manager = get_clob_ws_manager()

    queue: asyncio.Queue = asyncio.Queue()
    subscriber_id = manager.register_subscriber(queue.put_nowait)
    subscribed: list[str] = []
    try:
        position_ids = await _position_token_ids(wallet_address)
        subscribed = await manager.subscribe(
            list(dict.fromkeys([str(t) for t in token_ids] + position_ids))
        )

        pending: dict[str, dict] = {}
        last_emit = 0.0
        last_poll = 0.0
        last_heartbeat = time.monotonic()

        while True:
            connected = manager.is_connected
            try:
                tick = await asyncio.wait_for(queue.get(), timeout=0.1)
            except TimeoutError:
                tick = None

            if tick is not None and not is_stale_tick(tick.get("ts")):
                pending[tick["token_id"]] = tick

            now = time.monotonic()
            if connected:
                if pending and (tick is None or now - last_emit >= coalesce_seconds):
                    for item in pending.values():
                        yield _sse_frame(item)
                    pending.clear()
                    last_emit = now
            else:
                # Fallback: the CLOB WS is down — emit a poll snapshot
                # on the cadence the old polling loop used.
                pending.clear()
                if now - last_poll >= poll_interval_seconds:
                    last_poll = now
                    snapshot = await _fetch_token_prices(wallet_address, token_ids)
                    for token_id, price in snapshot.items():
                        yield _sse_frame(
                            {
                                "token_id": token_id,
                                "price": price,
                                "ts": utc_now().isoformat(),
                            }
                        )

            if now - last_heartbeat >= heartbeat_seconds:
                last_heartbeat = now
                yield ": ping\n\n"
    except asyncio.CancelledError:
        raise
    finally:
        manager.unregister_subscriber(subscriber_id)
        if subscribed:
            await manager.unsubscribe(subscribed)


@router.get("/prices/stream")
async def stream_prices(
    token_ids: str = "",
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    Server-sent events stream of real-time price ticks for the user's
    open positions plus client-supplied token ids (watchlist / visible
    markets).  Falls back to a 15s poll snapshot when the CLOB
    WebSocket is unavailable.
    """
    user_id = str(current_user.get("user_id") or "")
    wallet_address = current_user["wallet_address"]

    requested = [t.strip() for t in (token_ids or "").split(",") if t.strip()]
    if len(requested) > MAX_STREAM_TOKEN_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many token_ids (max {MAX_STREAM_TOKEN_IDS})",
        )

    if not await _sse_clients.acquire(user_id):
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent price streams for this user",
        )

    # Idempotent: the lifespan hook already started the manager.
    get_clob_ws_manager().start()

    async def guarded_generator():
        try:
            async for frame in _price_stream_frames(
                token_ids=requested,
                wallet_address=wallet_address,
            ):
                yield frame
        finally:
            await _sse_clients.release(user_id)

    return StreamingResponse(
        guarded_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── On-chain auto-redeem (plan 05) ──────────────────


class RedeemRequest(BaseModel):
    """Manual on-chain redemption trigger for a single resolved market."""

    market_id: str
    winner_outcome_index: int | None = None


class RedemptionAttemptResponse(BaseModel):
    """A recorded redemption attempt for a resolved-market position."""

    id: int
    user_id: int
    market_id: str
    collection_id: str
    tx_hash: str | None = None
    status: str
    amount: float
    error: str | None = None
    created_at: datetime


def _attempt_response(attempt) -> RedemptionAttemptResponse:
    """Serialize a RedemptionAttempt for the API."""
    return RedemptionAttemptResponse(
        id=attempt.id,
        user_id=attempt.user_id,
        market_id=attempt.market_id,
        collection_id=attempt.collection_id,
        tx_hash=attempt.tx_hash,
        status=attempt.status,
        amount=attempt.amount,
        error=attempt.error,
        created_at=attempt.created_at,
    )


@router.post("/redeem", response_model=RedemptionAttemptResponse)
async def redeem_market(
    request: RedeemRequest,
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    Manually trigger on-chain redemption for one resolved market.

    The winning outcome is validated against the market's
    resolved outcomePrices before any transaction is built.
    Dry-run mode (REDEEM_DRY_RUN) records the attempt without
    broadcasting a transaction.
    """
    from app.services.redemption_service import redeem_market_for_user

    attempt = await redeem_market_for_user(
        user_id=current_user["user_id"],
        wallet_address=current_user["wallet_address"],
        market_id=request.market_id,
        winner_outcome_index=request.winner_outcome_index,
    )
    if attempt is None:
        raise HTTPException(
            status_code=404,
            detail="No redeemable position found for this market",
        )
    return _attempt_response(attempt)


@router.get("/redemptions", response_model=list[RedemptionAttemptResponse])
async def get_redemptions(
    limit: int = 50,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Redemption attempt history for the authenticated user."""
    from app.models.redemption_attempt import RedemptionAttempt

    limit = max(1, min(limit, 200))
    rows = (
        db.query(RedemptionAttempt)
        .filter(RedemptionAttempt.user_id == current_user["user_id"])
        .order_by(RedemptionAttempt.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_attempt_response(row) for row in rows]
