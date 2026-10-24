"""Trade History, Leaderboard, Copy-Trading & Trade Execution API routes.

Manual execution runs the same pre-trade gate as copy trading
(``app.services.trading.pre_trade_gate``) before any order reaches the
exchange, and order submission is idempotent on the
``Idempotency-Key`` header so a retried request replays the
original result instead of placing a second order.
"""

import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_plan
from app.api.routes.auth.auth import get_current_user_from_token, get_optional_user_from_token
from app.models.notifications.notification_followed_trader import NotificationFollowedTrader
from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.stop_loss import StopLossOrder
from app.models.trading.take_profit import TakeProfitOrder
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.services.analytics.leaderboard_service import (
    fetch_leaderboard,
    fetch_trader_profile,
    fetch_trader_trades,
)
from app.services.market_data.polymarket_service import (
    get_polymarket_service,
)
from app.services.trading.copy_trade_service import (
    get_copy_trade_evaluation,
    get_copy_trade_history,
    get_daily_copy_pnl,
)
from app.services.trading.execution.async_io import run_blocking_execution
from app.services.trading.order_management_service import (
    cancel_stop_loss as cancel_stop_loss_service,
)
from app.services.trading.order_management_service import (
    cancel_take_profit as cancel_take_profit_service,
)
from app.services.trading.order_management_service import (
    commit_trading_settings,
    get_or_create_trading_settings,
    list_stop_losses,
    list_take_profits,
)
from app.services.trading.order_management_service import (
    set_stop_loss as set_stop_loss_service,
)
from app.services.trading.order_management_service import (
    set_take_profit as set_take_profit_service,
)
from app.services.trading.pre_trade_gate import (
    _to_float,
    apply_global_safety_caps,
)
from app.services.trading.trade_api_service import (
    follow_trader as follow_trader_service,
)
from app.services.trading.trade_api_service import (
    followed_wallets_for_user,
)
from app.services.trading.trade_api_service import (
    list_following as get_following_service,
)
from app.services.trading.trade_api_service import (
    list_following_feed as get_following_feed_service,
)
from app.services.trading.trade_api_service import (
    list_notification_following as get_notification_following_service,
)
from app.services.trading.trade_api_service import (
    notification_follow_trader as notification_follow_trader_service,
)
from app.services.trading.trade_api_service import (
    unfollow_notifications as unfollow_notifications_service,
)
from app.services.trading.trade_api_service import (
    unfollow_trader as unfollow_trader_service,
)
from app.services.users.entitlement_service import CAP_COPY_TRADING
from app.utils.database import get_db
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _safe_json_obj(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


# ────────────── Schemas ──────────────


class TradeRecord(BaseModel):
    id: str | None = None
    market: str | None = None
    market_slug: str | None = None
    condition_id: str | None = None
    outcome: str | None = None
    side: str | None = None
    size: float = 0.0
    price: float = 0.0
    type: str | None = None
    status: str | None = None
    timestamp: str | None = None
    fee: float = 0.0
    fee_rate_bps: float | None = None
    pnl: float | None = None
    transaction_hash: str | None = None
    source: str | None = None
    maker_address: str | None = None
    trader_side: str | None = None


class TradeHistoryResponse(BaseModel):
    wallet_address: str
    trades: list[TradeRecord] = []
    count: int = 0
    has_more: bool = False


class LeaderboardEntry(BaseModel):
    rank: int
    address: str
    display_name: str | None = None
    profit_loss: float = 0.0
    volume: float = 0.0
    markets_traded: int = 0
    win_rate: float | None = None
    positions_value: float = 0.0
    pnl_24h: float = 0.0
    pnl_7d: float = 0.0
    pnl_30d: float = 0.0
    volume_24h: float = 0.0
    profile_image: str | None = None
    is_followed: bool = False
    is_notification_followed: bool = False
    quality_score: float | None = None
    quality_tier: str | None = None


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry] = []
    period: str = "all_time"
    updated_at: str
    total: int = 0


class TraderProfileResponse(BaseModel):
    wallet_address: str
    display_name: str | None = None
    profit_loss: float = 0.0
    volume: float = 0.0
    markets_traded: int = 0
    win_rate: float | None = None
    positions: list[dict] = []
    recent_trades: list[dict] = []
    profile_image: str | None = None
    trade_stats: dict | None = None  # Real stats computed from on-chain data


class FollowTraderRequest(BaseModel):
    max_position_size: float | None = Field(default=None, gt=0)
    trader_alias: str | None = Field(default=None, max_length=100)
    sizing_mode: str | None = Field(
        default=None,
        pattern="^(inherit_global|fixed_amount|trader_wallet_ratio|kelly)$",
    )
    fixed_trade_amount_override: float | None = Field(default=None, gt=0)
    copy_wallet_mode: str | None = Field(
        default=None,
        pattern="^(dynamic_main_wallet_percentage|fixed_snapshot_amount)$",
    )
    copy_wallet_percentage: float | None = Field(default=None, gt=0, le=100)
    copy_wallet_fixed_amount: float | None = Field(default=None, gt=0)


class FollowedTraderResponse(BaseModel):
    id: int
    trader_wallet: str
    trader_alias: str | None = None
    display_name: str | None = None
    is_active: bool = True
    max_position_size: float | None = None
    sizing_mode: str = "inherit_global"
    fixed_trade_amount_override: float | None = None
    copy_wallet_mode: str = "dynamic_main_wallet_percentage"
    copy_wallet_percentage: float = 100.0
    copy_wallet_fixed_amount: float | None = None
    created_at: str


class NotificationFollowRequest(BaseModel):
    feed_enabled: bool | None = None
    email_enabled: bool | None = None


class NotificationFollowedTraderResponse(BaseModel):
    id: int
    trader_wallet: str
    is_active: bool = True
    feed_enabled: bool = True
    email_enabled: bool = False
    created_at: str


class FollowingFeedEventResponse(BaseModel):
    id: int
    trader_wallet: str
    event_type: str
    market_id: str
    token_id: str
    side: str
    size: float
    price: float
    prev_net_size: float
    new_net_size: float
    source_trade_history_id: int | None = None
    email_status: str
    email_error: str | None = None
    emailed_at: str | None = None
    created_at: str


class CopyTradeRecord(BaseModel):
    id: int
    trader_wallet: str
    market_id: str
    side: str
    size: float
    price: float
    status: str
    pnl: float | None = None
    timestamp: str
    source_trade_history_id: int | None = None
    trader_trade_notional: float | None = None
    trader_wallet_balance: float | None = None
    copy_wallet_base: float | None = None
    sizing_mode_applied: str | None = None
    copy_wallet_mode_applied: str | None = None
    calculation_warning: str | None = None
    calculation_details: dict | None = None


class CopyEvaluationRowResponse(BaseModel):
    source_trade_id: int
    source_trade_id_ext: str | None = None
    source_timestamp: str | None = None
    market_id: str
    side: str
    price: float
    source_trade_notional: float
    copy_trade_id: int | None = None
    copy_timestamp: str | None = None
    copied_size: float | None = None
    copy_status: str = "not_copied"
    order_hash: str | None = None
    trader_wallet_balance: float | None = None
    copy_wallet_base: float | None = None
    ratio: float | None = None
    sizing_mode_applied: str | None = None
    copy_wallet_mode_applied: str | None = None
    warning: str | None = None


class CopyEvaluationResponse(BaseModel):
    wallet: str
    rows: list[CopyEvaluationRowResponse]
    count: int
    updated_at: str


class ExecuteTradeRequest(BaseModel):
    """Request to execute a Buy/Sell on Polymarket."""

    execution_mode: Literal["live"]
    token_id: str = Field(..., description="Polymarket token ID (condition token)")
    market_id: str = Field(default="", description="Market slug or condition ID for record-keeping")
    market_title: str = Field(default="", description="Human-readable market title")
    side: str = Field(..., pattern="^(BUY|SELL)$", description="BUY or SELL")
    price: float = Field(..., gt=0, le=1, description="Limit price (0-1)")
    size: float = Field(
        ...,
        gt=0,
        description="BUY: amount in USDC, SELL: number of shares",
    )
    outcome: str = Field(default="", description="Yes/No outcome label")


class ExecuteTradeResponse(BaseModel):
    success: bool
    order_hash: str | None = None
    trade_id: int | None = None
    status: str
    side: str
    size: float
    price: float
    error: str | None = None


# ────────────── Trade History ──────────────


@router.get("/history", response_model=TradeHistoryResponse)
async def get_trade_history(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get the authenticated user's trade history from Polymarket."""
    wallet_address = current_user["wallet_address"]
    service = get_polymarket_service()

    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    trades_raw = await service.get_trade_history(
        wallet_address,
        private_key=pk,
        clob_creds=creds,
        limit=limit,
        offset=offset,
    )

    trades = [TradeRecord(**t) for t in trades_raw]

    return TradeHistoryResponse(
        wallet_address=wallet_address,
        trades=trades,
        count=len(trades),
        has_more=len(trades) == limit,
    )


# ────────────── Trade Execution ──────────────


@router.post("/execute", response_model=ExecuteTradeResponse)
async def execute_trade(
    body: ExecuteTradeRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Submit a real order with durable identity and authenticated risk checks."""
    from decimal import ROUND_DOWN, Decimal

    from app.services.trading.execution.live import place_live_order, require_enabled
    from app.services.trading.live_trading_policy import LiveTradingBlocked

    try:
        require_enabled("manual")
        if db is None:
            raise LiveTradingBlocked("DURABLE_EXECUTION_CONTEXT_REQUIRED")
        if not idempotency_key:
            raise HTTPException(422, "Idempotency-Key is required for a live order")
        # Existing manual API: buys are collateral notional, sells are shares.
        shares = Decimal(str(body.size))
        if body.side == "BUY":
            shares /= Decimal(str(body.price))
        shares = shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        result = await run_blocking_execution(
            place_live_order,
            db,
            user_id=current_user["user_id"],
            token_id=body.token_id,
            market_id=body.market_id,
            side=body.side,
            price=body.price,
            size=float(shares),
            idempotency_key=idempotency_key,
            strategy="manual",
            order_type="FOK" if body.side == "SELL" else "GTC",
        )
    except LiveTradingBlocked as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return ExecuteTradeResponse(
        success=result["success"],
        order_hash=result["order_hash"],
        trade_id=result["trade_id"],
        status=result["status"],
        side=body.side,
        size=body.size,
        price=body.price,
        error=result.get("error"),
    )


@router.post("/cash-out")
async def cash_out_position(
    body: ExecuteTradeRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Cash out (sell) an existing position at a specified price.
    This is a convenience wrapper around the execute endpoint
    that always creates a SELL order.
    """
    body.side = "SELL"
    return await execute_trade(
        body,
        idempotency_key=idempotency_key,
        current_user=current_user,
        db=db,
    )


# ────────────── Leaderboard ──────────────


@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    limit: int = Query(default=25, le=1000),
    period: str = Query(default="all_time", pattern="^(24h|7d|30d|all_time)$"),
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Real-time leaderboard of top Polymarket traders.
    Fetches from multiple Polymarket data sources with Redis caching.
    Supports period filtering (24h, 7d, 30d, all_time).
    """
    user_id = current_user.get("user_id") if current_user else None

    # Fetch leaderboard data
    raw_entries = await fetch_leaderboard(limit=limit, period=period)

    # Get user's followed traders for copy flag and notification-follow flag
    followed_wallets = set()
    notification_followed_wallets = set()
    if user_id:
        copy_wallets, notification_wallets = followed_wallets_for_user(db, user_id)
        followed_wallets = {w.lower() for w in copy_wallets}
        notification_followed_wallets = {w.lower() for w in notification_wallets}

    entries = []
    for idx, item in enumerate(raw_entries):
        addr = item.get("address", "unknown")
        entries.append(
            LeaderboardEntry(
                rank=idx + 1,
                address=addr,
                display_name=item.get("display_name"),
                profit_loss=float(item.get("profit_loss", 0)),
                volume=float(item.get("volume", 0)),
                markets_traded=int(item.get("markets_traded", 0)),
                win_rate=item.get("win_rate"),
                positions_value=float(item.get("positions_value", 0)),
                pnl_24h=float(item.get("pnl_24h", 0)),
                pnl_7d=float(item.get("pnl_7d", 0)),
                pnl_30d=float(item.get("pnl_30d", 0)),
                volume_24h=float(item.get("volume_24h", 0)),
                profile_image=item.get("profile_image"),
                is_followed=addr.lower() in followed_wallets,
                is_notification_followed=(addr.lower() in notification_followed_wallets),
                quality_score=item.get("quality_score"),
                quality_tier=item.get("quality_tier"),
            )
        )

    return LeaderboardResponse(
        entries=entries,
        period=period,
        updated_at=utc_now().isoformat(),
        total=len(entries),
    )


# ────────────── Trader Profile ──────────────


@router.get("/trader/{wallet}", response_model=TraderProfileResponse)
async def get_trader_profile(
    wallet: str,
):
    """Get detailed profile for a specific trader with real trade statistics (public)."""
    profile = await fetch_trader_profile(wallet)
    if not profile:
        raise HTTPException(status_code=404, detail="Trader not found")

    # Fetch real trade data and compute actual stats
    trade_data = await fetch_trader_trades(wallet, max_trades=500)
    trade_stats = trade_data.get("stats", {})

    # Use real win rate if available, else keep leaderboard value
    real_win_rate = trade_stats.get("win_rate")
    real_markets = trade_stats.get("unique_markets", 0)
    trade_stats.get("total_trades", 0)

    return TraderProfileResponse(
        wallet_address=wallet,
        display_name=profile.get("display_name"),
        profit_loss=float(profile.get("profit_loss", 0)),
        volume=float(profile.get("volume", 0)),
        markets_traded=max(int(profile.get("markets_traded", 0)), real_markets),
        win_rate=real_win_rate if real_win_rate and real_win_rate > 0 else profile.get("win_rate"),
        positions=profile.get("positions", []),
        recent_trades=profile.get("recent_trades", []),
        profile_image=profile.get("profile_image"),
        trade_stats=trade_stats if trade_stats.get("total_trades", 0) > 0 else None,
    )


# ────────────── Follow / Unfollow Traders ──────────────


def _followed_to_response(record: FollowedTrader) -> FollowedTraderResponse:
    return FollowedTraderResponse(
        id=record.id,
        trader_wallet=record.trader_wallet,
        trader_alias=record.trader_alias,
        is_active=record.is_active,
        max_position_size=record.max_position_size,
        sizing_mode=record.sizing_mode or "inherit_global",
        fixed_trade_amount_override=record.fixed_trade_amount_override,
        copy_wallet_mode=record.copy_wallet_mode or "dynamic_main_wallet_percentage",
        copy_wallet_percentage=record.copy_wallet_percentage or 100.0,
        copy_wallet_fixed_amount=record.copy_wallet_fixed_amount,
        created_at=record.created_at.isoformat(),
    )


def _notification_followed_to_response(
    record: NotificationFollowedTrader,
) -> NotificationFollowedTraderResponse:
    return NotificationFollowedTraderResponse(
        id=record.id,
        trader_wallet=record.trader_wallet,
        is_active=record.is_active,
        feed_enabled=record.feed_enabled,
        email_enabled=record.email_enabled,
        created_at=record.created_at.isoformat(),
    )


@router.post(
    "/follow/{wallet}",
    response_model=FollowedTraderResponse,
    dependencies=[Depends(require_plan(CAP_COPY_TRADING))],
)
async def follow_trader(
    wallet: str,
    body: FollowTraderRequest = FollowTraderRequest(),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Start following a trader for copy-trading."""
    user_id = current_user.get("user_id")
    provided_fields = body.model_fields_set
    record = follow_trader_service(
        db,
        user_id,
        wallet,
        changes=body.model_dump(),
        provided_fields=provided_fields,
    )

    # Dynamically add wallet to trade monitor
    try:
        from app.services.trading.trade_monitor import (
            add_watched_wallet,
            seed_wallet_cursor,
        )

        add_watched_wallet(wallet.lower())
        # Seed the per-wallet cursor with the wallet's
        # newest trade so the monitor's first WS subscribe
        # / poll does not replay their trade history as
        # new trades (and place duplicate copy orders).
        asyncio.create_task(seed_wallet_cursor(wallet.lower()))
    except Exception as e:
        logger.warning(f"Could not add wallet to monitor: {e}")

    return _followed_to_response(record)


@router.delete("/follow/{wallet}")
async def unfollow_trader(
    wallet: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Stop following a trader."""
    user_id = current_user.get("user_id")

    found, remove_from_monitor = unfollow_trader_service(db, user_id, wallet)
    if not found:
        raise HTTPException(status_code=404, detail="Not following this trader")

    # Remove from monitor only if no active copy or notification subscribers remain.
    if remove_from_monitor:
        try:
            from app.services.trading.trade_monitor import remove_watched_wallet

            remove_watched_wallet(wallet.lower())
        except Exception as e:
            logger.warning(f"Could not remove wallet from monitor: {e}")

    return {"status": "unfollowed", "wallet": wallet}


@router.get("/following", response_model=list[FollowedTraderResponse])
async def get_following(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get list of traders the user is currently following."""
    user_id = current_user.get("user_id")

    records = get_following_service(db, user_id)

    return [_followed_to_response(r) for r in records]


@router.post(
    "/notification-follow/{wallet}",
    response_model=NotificationFollowedTraderResponse,
)
async def notification_follow_trader(
    wallet: str,
    body: NotificationFollowRequest = NotificationFollowRequest(),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Start following a trader for opened/closed position notifications."""
    user_id = current_user.get("user_id")
    provided_fields = body.model_fields_set
    normalized_wallet = wallet.lower()
    record = notification_follow_trader_service(
        db,
        user_id,
        wallet,
        feed_enabled=body.feed_enabled,
        email_enabled=body.email_enabled,
        provided_fields=provided_fields,
    )

    try:
        from app.services.trading.trade_monitor import add_watched_wallet

        add_watched_wallet(normalized_wallet)
    except Exception as e:
        logger.warning(f"Could not add notification wallet to monitor: {e}")

    return _notification_followed_to_response(record)


@router.delete("/notification-follow/{wallet}")
async def notification_unfollow_trader(
    wallet: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Stop notification-following a trader."""
    user_id = current_user.get("user_id")
    normalized_wallet = wallet.lower()
    found, remove_from_monitor = unfollow_notifications_service(db, user_id, normalized_wallet)
    if not found:
        raise HTTPException(
            status_code=404,
            detail="Not following this trader for notifications",
        )

    if remove_from_monitor:
        try:
            from app.services.trading.trade_monitor import remove_watched_wallet

            remove_watched_wallet(normalized_wallet)
        except Exception as e:
            logger.warning(f"Could not remove notification wallet from monitor: {e}")

    return {"status": "notification_unfollowed", "wallet": normalized_wallet}


@router.get(
    "/notification-following",
    response_model=list[NotificationFollowedTraderResponse],
)
async def get_notification_following(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List active notification-followed traders for the current user."""
    user_id = current_user.get("user_id")
    rows = get_notification_following_service(db, user_id)
    return [_notification_followed_to_response(r) for r in rows]


@router.get("/following-feed", response_model=list[FollowingFeedEventResponse])
async def get_following_feed(
    limit: int = Query(default=50, le=200),
    wallet: str | None = Query(default=None),
    event_type: str | None = Query(default=None, pattern="^(opened|closed)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get chronological followed-trader opened/closed events for dashboard feed."""
    user_id = current_user.get("user_id")
    rows = get_following_feed_service(
        db,
        user_id,
        wallet=wallet,
        event_type=event_type,
        limit=limit,
    )

    return [
        FollowingFeedEventResponse(
            id=r.id,
            trader_wallet=r.trader_wallet,
            event_type=r.event_type,
            market_id=r.market_id or "",
            token_id=r.token_id or "",
            side=r.side or "",
            size=float(r.size or 0),
            price=float(r.price or 0),
            prev_net_size=float(r.prev_net_size or 0),
            new_net_size=float(r.new_net_size or 0),
            source_trade_history_id=r.source_trade_history_id,
            email_status=r.email_status or "skipped",
            email_error=r.email_error,
            emailed_at=r.emailed_at.isoformat() if r.emailed_at else None,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


# ────────────── Copy-Trade History ──────────────


@router.get("/copy-trades", response_model=list[CopyTradeRecord])
async def get_copy_trades(
    limit: int = Query(default=50, le=200),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get history of copy trades executed for the user."""
    user_id = current_user.get("user_id")
    trades = await get_copy_trade_history(db, user_id, limit=limit)

    return [
        CopyTradeRecord(
            id=t.get("id"),
            trader_wallet=t.get("copied_from_wallet") or "",
            market_id=t.get("market_id") or "",
            side=t.get("action") or "",
            size=float(t.get("amount") or 0),
            price=float(t.get("price") or 0),
            status=t.get("status") or "unknown",
            pnl=float(t["pnl"]) if t.get("pnl") is not None else None,
            timestamp=t.get("executed_at") or t.get("created_at") or "",
            source_trade_history_id=t.get("source_trade_history_id"),
            trader_trade_notional=(
                float(t["trader_trade_notional"])
                if t.get("trader_trade_notional") is not None
                else None
            ),
            trader_wallet_balance=(
                float(t["trader_wallet_balance"])
                if t.get("trader_wallet_balance") is not None
                else None
            ),
            copy_wallet_base=(
                float(t["copy_wallet_base"]) if t.get("copy_wallet_base") is not None else None
            ),
            sizing_mode_applied=t.get("sizing_mode_applied"),
            copy_wallet_mode_applied=t.get("copy_wallet_mode_applied"),
            calculation_warning=t.get("calculation_warning"),
            calculation_details=_safe_json_obj(t.get("calculation_details")),
        )
        for t in trades
    ]


@router.get("/copy-evaluation/{wallet}", response_model=CopyEvaluationResponse)
async def get_copy_evaluation(
    wallet: str,
    limit: int = Query(default=50, le=200),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Side-by-side source trades and copied outcomes for a specific trader."""
    user_id = current_user.get("user_id")
    rows = await get_copy_trade_evaluation(db, user_id, wallet, limit=limit)
    return CopyEvaluationResponse(
        wallet=wallet.lower(),
        rows=[CopyEvaluationRowResponse(**r) for r in rows],
        count=len(rows),
        updated_at=utc_now().isoformat(),
    )


@router.get("/copy-trades/pnl")
async def get_copy_trade_pnl(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get daily PnL from copy trades."""
    user_id = current_user.get("user_id")
    daily_pnl = await get_daily_copy_pnl(db, user_id)
    return {"daily_pnl": daily_pnl}


# ────────────── Stop-Loss Orders ──────────────


class SetStopLossRequest(BaseModel):
    """Create or update a stop-loss order."""

    token_id: str = Field(..., description="CLOB token ID")
    market_id: str = Field(default="", description="Market condition ID")
    market_title: str = Field(default="", description="Human-readable title")
    outcome: str = Field(default="", description="Yes/No outcome label")
    size: float = Field(..., gt=0, description="Number of shares")
    stop_price: float = Field(..., gt=0, lt=1, description="Trigger price (0-1)")


class StopLossResponse(BaseModel):
    id: int
    token_id: str
    market_id: str
    market_title: str
    outcome: str
    size: float
    stop_price: float
    status: str
    order_hash: str | None = None
    executed_price: float | None = None
    triggered_at: str | None = None
    created_at: str
    updated_at: str


def _sl_to_response(sl: StopLossOrder) -> StopLossResponse:
    return StopLossResponse(
        id=sl.id,
        token_id=sl.token_id,
        market_id=sl.market_id or "",
        market_title=sl.market_title or "",
        outcome=sl.outcome or "",
        size=sl.size,
        stop_price=sl.stop_price,
        status=sl.status,
        order_hash=sl.order_hash,
        executed_price=sl.executed_price,
        triggered_at=sl.triggered_at.isoformat() if sl.triggered_at else None,
        created_at=sl.created_at.isoformat() if sl.created_at else "",
        updated_at=sl.updated_at.isoformat() if sl.updated_at else "",
    )


@router.post("/stop-loss", response_model=StopLossResponse)
async def set_stop_loss(
    body: SetStopLossRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Create or update a stop-loss order for a position.
    If an active stop-loss already exists for the same token, it is updated.
    The background monitor checks prices every ~10s and triggers a sell
    when price <= stop_price.
    """
    user_id = current_user.get("user_id")

    sl = set_stop_loss_service(
        db,
        user_id=user_id,
        token_id=body.token_id,
        market_id=body.market_id,
        market_title=body.market_title,
        outcome=body.outcome,
        size=body.size,
        stop_price=body.stop_price,
        now=utc_now(),
    )
    return _sl_to_response(sl)


@router.get("/stop-loss", response_model=list[StopLossResponse])
async def get_stop_losses(
    status: str = Query(default="active", pattern="^(active|triggered|cancelled|failed|all)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List stop-loss orders for the current user."""
    user_id = current_user.get("user_id")
    orders = list_stop_losses(db, user_id, None if status == "all" else status)
    return [_sl_to_response(o) for o in orders]


@router.delete("/stop-loss/{stop_loss_id}")
async def cancel_stop_loss(
    stop_loss_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Cancel an active stop-loss order."""
    user_id = current_user.get("user_id")
    state, sl = cancel_stop_loss_service(
        db,
        order_id=stop_loss_id,
        user_id=user_id,
        now=utc_now(),
    )
    if state == "missing":
        raise HTTPException(status_code=404, detail="Stop-loss order not found")
    if state == "inactive":
        raise HTTPException(status_code=400, detail=f"Cannot cancel – status is '{sl.status}'")
    return {"status": "cancelled", "id": stop_loss_id}


# ────────────── Take-Profit Orders ──────────────


class SetTakeProfitRequest(BaseModel):
    """Create or update a take-profit order."""

    token_id: str = Field(..., description="CLOB token ID")
    market_id: str = Field(default="", description="Market condition ID")
    market_title: str = Field(default="", description="Human-readable title")
    outcome: str = Field(default="", description="Yes/No outcome label")
    size: float = Field(..., gt=0, description="Number of shares")
    take_profit_price: float = Field(
        ..., gt=0, lt=1, description="Trigger price (0-1) – sells when price >= this"
    )


class TakeProfitResponse(BaseModel):
    id: int
    token_id: str
    market_id: str
    market_title: str
    outcome: str
    size: float
    take_profit_price: float
    status: str
    order_hash: str | None = None
    executed_price: float | None = None
    triggered_at: str | None = None
    created_at: str
    updated_at: str


def _tp_to_response(tp: TakeProfitOrder) -> TakeProfitResponse:
    return TakeProfitResponse(
        id=tp.id,
        token_id=tp.token_id,
        market_id=tp.market_id or "",
        market_title=tp.market_title or "",
        outcome=tp.outcome or "",
        size=tp.size,
        take_profit_price=tp.take_profit_price,
        status=tp.status,
        order_hash=tp.order_hash,
        executed_price=tp.executed_price,
        triggered_at=tp.triggered_at.isoformat() if tp.triggered_at else None,
        created_at=tp.created_at.isoformat() if tp.created_at else "",
        updated_at=tp.updated_at.isoformat() if tp.updated_at else "",
    )


@router.post("/take-profit", response_model=TakeProfitResponse)
async def set_take_profit(
    body: SetTakeProfitRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Create or update a take-profit order for a position.
    If an active take-profit already exists for the same token, it is updated.
    The background monitor checks prices every ~10s and triggers a sell
    when price >= take_profit_price.
    """
    user_id = current_user.get("user_id")

    tp = set_take_profit_service(
        db,
        user_id=user_id,
        token_id=body.token_id,
        market_id=body.market_id,
        market_title=body.market_title,
        outcome=body.outcome,
        size=body.size,
        take_profit_price=body.take_profit_price,
        now=utc_now(),
    )
    return _tp_to_response(tp)


@router.get("/take-profit", response_model=list[TakeProfitResponse])
async def get_take_profits(
    status: str = Query(default="active", pattern="^(active|triggered|cancelled|failed|all)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List take-profit orders for the current user."""
    user_id = current_user.get("user_id")
    orders = list_take_profits(db, user_id, None if status == "all" else status)
    return [_tp_to_response(o) for o in orders]


@router.delete("/take-profit/{take_profit_id}")
async def cancel_take_profit(
    take_profit_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Cancel an active take-profit order."""
    user_id = current_user.get("user_id")
    state, tp = cancel_take_profit_service(
        db,
        order_id=take_profit_id,
        user_id=user_id,
        now=utc_now(),
    )
    if state == "missing":
        raise HTTPException(status_code=404, detail="Take-profit order not found")
    if state == "inactive":
        raise HTTPException(status_code=400, detail=f"Cannot cancel – status is '{tp.status}'")
    return {"status": "cancelled", "id": take_profit_id}


# ────────────── Emergency Stop / Panic Sell ──────────────


@router.post("/emergency-stop", status_code=202)
async def emergency_stop(
    close_positions: bool = Query(default=False),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Persist the wallet halt and cancellation work before returning an ID."""
    from app.services.trading.trading_halt_service import operation_status, request_halt

    user_id = current_user["user_id"]
    operations = request_halt(db, user_id=user_id, close_positions=close_positions)
    return operation_status(db, operation_id=operations[0].id, user_id=user_id)


@router.get("/emergency-stop")
async def latest_emergency_stop(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    from app.models.trading.trading_control import EmergencyHaltOperation
    from app.services.trading.trading_halt_service import operation_status

    user_id = current_user["user_id"]
    operation = (
        db.query(EmergencyHaltOperation)
        .filter(EmergencyHaltOperation.user_id == user_id)
        .order_by(EmergencyHaltOperation.created_at.desc(), EmergencyHaltOperation.id.desc())
        .first()
    )
    if operation is None:
        return None
    return operation_status(db, operation_id=operation.id, user_id=user_id)


@router.get("/emergency-stop/{operation_id}")
async def emergency_stop_status(
    operation_id: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    from app.services.trading.trading_halt_service import operation_status

    result = operation_status(db, operation_id=operation_id, user_id=current_user["user_id"])
    if result is None:
        raise HTTPException(status_code=404, detail="Halt operation not found")
    return result


# Versioned safety endpoints share the same ownership and persistence handlers.
safety_router = APIRouter(prefix="/api/v1/trading", tags=["trading-safety"])
safety_router.add_api_route("/emergency-stop", emergency_stop, methods=["POST"], status_code=202)
safety_router.add_api_route("/emergency-stop", latest_emergency_stop, methods=["GET"])
safety_router.add_api_route(
    "/emergency-stop/{operation_id}", emergency_stop_status, methods=["GET"]
)


@router.post("/resume-trading")
async def resume_trading(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Legacy resume cannot override durable recovery and release checks."""
    raise HTTPException(status_code=409, detail="RECOVERY_REQUIRES_VERIFIED_EXECUTION_GATEWAY")


# ────────────── Trader Quality Scoring ──────────────


class TraderQualityResponse(BaseModel):
    wallet_address: str
    quality_score: float | None = None
    consistency_score: float | None = None
    risk_adjusted_score: float | None = None
    activity_score: float | None = None
    win_rate_score: float | None = None
    quality_tier: str | None = None


@router.get("/trader/{wallet}/quality", response_model=TraderQualityResponse)
async def get_trader_quality_score(
    wallet: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get quality score breakdown for a trader."""
    from app.services.analytics.trader_quality_service import get_trader_quality

    scores = get_trader_quality(db, wallet)
    if not scores:
        raise HTTPException(status_code=404, detail="Trader not found or insufficient data")
    return TraderQualityResponse(wallet_address=wallet.lower(), **scores)


@router.post("/traders/rescore")
async def rescore_all_traders(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Recompute quality scores for all tracked traders."""
    from app.services.analytics.trader_quality_service import score_all_traders

    count = score_all_traders(db)
    return {"scored": count, "message": f"Quality scores updated for {count} traders"}


# ────────────── Arbitrage Detection ──────────────


class ArbitrageOpportunity(BaseModel):
    type: str
    market_id: str
    market_title: str
    profit_pct: float | None = None
    spread_pct: float | None = None
    total_cost: float | None = None
    bid: float | None = None
    ask: float | None = None
    token_id: str | None = None
    outcome: str | None = None
    detected_at: str


@router.get("/arbitrage/opportunities", response_model=list[ArbitrageOpportunity])
async def get_arbitrage_opportunities(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get recently detected arbitrage opportunities."""
    from app.services.analytics.arbitrage_service import get_recent_opportunities

    return get_recent_opportunities()


@router.post("/arbitrage/scan")
async def trigger_arbitrage_scan(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Trigger an immediate arbitrage scan."""
    from app.services.analytics.arbitrage_service import scan_for_arbitrage

    opportunities = await scan_for_arbitrage()
    return {
        "found": len(opportunities),
        "opportunities": opportunities[:20],
    }


# ────────────── Execution Analytics (plan 03) ──────────────


class ExecutionStrategyStats(BaseModel):
    """Rolling-window execution statistics for one strategy."""

    strategy: str
    submissions: int = 0
    filled: int = 0
    fill_rate: float | None = None
    avg_slippage_bps: float | None = None
    median_slippage_bps: float | None = None
    total_fees: float = 0.0
    gross_realized_pnl: float = 0.0
    net_realized_pnl: float = 0.0
    fee_drag_ratio: float | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    avg_fee: float | None = None
    avg_risk_per_trade: float | None = None
    edge_raw: float | None = None
    edge_score: float | None = None
    missing_fee: int = 0
    missing_expected_price: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0


class ExecutionSummaryResponse(BaseModel):
    user_id: int
    window_days: int
    generated_at: str
    per_strategy: dict[str, ExecutionStrategyStats] = {}
    totals: ExecutionStrategyStats
    data_quality: dict[str, int] = {}


class EdgeScoreRow(BaseModel):
    strategy: str
    edge_score: float | None = None
    edge_raw: float | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    avg_fee: float | None = None
    avg_risk_per_trade: float | None = None
    filled: int = 0
    closed_trades: int = 0


class EdgeScoreResponse(BaseModel):
    user_id: int
    window_days: int
    generated_at: str
    strategies: list[EdgeScoreRow] = []


class ExecutionTradeRecord(BaseModel):
    id: int
    market_id: str
    token_id: str | None = None
    action: str
    strategy_source: str | None = None
    status: str
    expected_price: float | None = None
    expected_size: float | None = None
    filled_price: float | None = None
    filled_size: float | None = None
    fee_paid: float | None = None
    slippage_bps: float | None = None
    latency_ms: float | None = None
    pnl: float | None = None
    order_hash: str | None = None
    executed_at: str | None = None
    created_at: str | None = None


class ExecutionTradesResponse(BaseModel):
    user_id: int
    strategy: str | None = None
    count: int
    trades: list[ExecutionTradeRecord] = []


@router.get("/analytics/summary", response_model=ExecutionSummaryResponse)
async def get_execution_analytics_summary(
    window_days: int = Query(default=30, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Execution-quality summary (slippage, fee drag, fill rate) per strategy.

    Rolling window over orders the user submitted; pre-migration
    trades lack expected-price data and are reported in the
    data_quality block instead of the metrics.
    """
    from app.services.analytics.execution_analytics import get_execution_summary

    user_id = current_user.get("user_id")
    summary = get_execution_summary(db, user_id, window_days=window_days)
    return ExecutionSummaryResponse(**summary)


@router.get("/analytics/edge-score", response_model=EdgeScoreResponse)
async def get_execution_edge_scores(
    window_days: int = Query(default=30, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Edge Score per strategy, normalized to 0–100 (50 = break-even)."""
    from app.services.analytics.execution_analytics import get_edge_scores

    user_id = current_user.get("user_id")
    rows = get_edge_scores(db, user_id, window_days=window_days)
    return EdgeScoreResponse(
        user_id=user_id,
        window_days=window_days,
        generated_at=utc_now().isoformat(),
        strategies=[EdgeScoreRow(**row) for row in rows],
    )


class StrategyBreakdownRow(BaseModel):
    """One strategy's realized performance over the window (plan 03 Phase A).

    A flat row per strategy, ordered best net realized P&L first, with
    the attribution coverage that makes the numbers auditable.
    """

    strategy: str
    submissions: int = 0
    filled: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    gross_realized_pnl: float = 0.0
    net_realized_pnl: float = 0.0
    total_fees: float = 0.0
    avg_slippage_bps: float | None = None
    edge_score: float | None = None


class StrategyBreakdownResponse(BaseModel):
    user_id: int
    window_days: int
    generated_at: str
    strategies: list[StrategyBreakdownRow] = []
    data_quality: dict[str, int] = {}


@router.get("/analytics/by-strategy", response_model=StrategyBreakdownResponse)
async def get_strategy_breakdown(
    window_days: int = Query(default=30, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Per-strategy realized P&L attribution (plan 03 Phase A).

    Flat, best-first list of every strategy the user traded in the
    window, plus the count of trades that still carry no
    ``strategy_source`` — the attribution gap this endpoint exists to
    make visible.
    """
    from app.services.analytics.execution_analytics import get_strategy_breakdown

    user_id = current_user.get("user_id")
    payload = get_strategy_breakdown(db, user_id, window_days=window_days)
    return StrategyBreakdownResponse(
        user_id=user_id,
        window_days=payload["window_days"],
        generated_at=payload["generated_at"],
        strategies=[StrategyBreakdownRow(**row) for row in payload["strategies"]],
        data_quality=payload["data_quality"],
    )


@router.get("/analytics/trades", response_model=ExecutionTradesResponse)
async def get_execution_trade_details(
    strategy: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Per-trade execution detail (expected vs actual fill), newest first."""
    from app.services.analytics.execution_analytics import (
        STRATEGY_SOURCES,
        get_trade_details,
    )

    if strategy is not None and strategy not in STRATEGY_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategy '{strategy}'. Valid: {', '.join(STRATEGY_SOURCES)}",
        )
    user_id = current_user.get("user_id")
    trades = get_trade_details(db, user_id, strategy=strategy, limit=limit)
    return ExecutionTradesResponse(
        user_id=user_id,
        strategy=strategy,
        count=len(trades),
        trades=[ExecutionTradeRecord(**row) for row in trades],
    )


# ────────────── Kelly Size Suggestion (plan 04) ──────────────


class SizeSuggestionResponse(BaseModel):
    """Kelly-criterion size suggestion for the manual trade ticket."""

    suggested_size_usdc: float = Field(
        0.0,
        description="Suggested order size in USDC after all caps",
    )
    edge_probability: float | None = Field(
        None,
        description="Resolved win probability (0-1), or null when no edge estimate exists",
    )
    kelly_fraction: float = Field(
        0.25,
        description="Fractional-Kelly multiplier applied (user setting)",
    )
    capped_by: str | None = Field(
        None,
        description="Limit that capped the suggestion "
        "(max_position_size, min_order, safety caps, …)",
    )
    edge_source: str | None = Field(
        None,
        description="Edge estimate source (ai_assessment, trader_win_rate, user_win_rate)",
    )
    bankroll_usdc: float | None = Field(
        None,
        description="Bankroll the size was computed from (USDC balance + unrealized PnL)",
    )
    kelly_size_usdc: float | None = Field(
        None,
        description="Raw Kelly size before the global safety caps",
    )


@router.get("/size-suggestion", response_model=SizeSuggestionResponse)
async def get_size_suggestion(
    market_id: str = Query(
        ...,
        description="Market slug or condition ID to size for",
    ),
    side: str = Query(
        "BUY",
        pattern="^(BUY|SELL)$",
        description="Side the suggestion is sized for",
    ),
    price: float | None = Query(
        None,
        gt=0,
        le=1,
        description="Limit price (0-1); fetched from the order book when omitted",
    ),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Kelly-criterion size suggestion for the manual trade modal.

    Resolves the edge probability for the market (AI assessment
    confidence, then historical win rate), computes the fractional
    Kelly wager against the user's bankroll (USDC balance +
    unrealized PnL), and reports the size after the global safety
    caps together with what capped it.  The suggestion is advisory:
    the amount is pre-filled editable in the trade modal and the
    execute endpoint re-runs the full safety gate on submission.
    """
    from app.services.trading import kelly_service

    user_id = current_user.get("user_id")
    wallet_address = current_user["wallet_address"]

    settings = get_or_create_trading_settings(db, user_id, commit_new=True)

    multiplier = _to_float(
        settings.kelly_fraction,
        kelly_service.DEFAULT_KELLY_MULTIPLIER,
    )

    p_market, edge_source = kelly_service.resolve_edge_probability(
        db,
        user_id,
        market_id,
    )

    if p_market is None:
        logger.info(
            "Kelly size suggestion: no edge estimate user=%d market=%s",
            user_id,
            market_id,
        )
        return SizeSuggestionResponse(
            suggested_size_usdc=0.0,
            edge_probability=None,
            kelly_fraction=multiplier,
            capped_by="no_edge",
            edge_source=None,
            bankroll_usdc=None,
            kelly_size_usdc=None,
        )

    # Resolve the price: the caller's limit price wins; otherwise
    # fetch the market's current YES price from the order book.
    resolved_price = _to_float(price)
    if resolved_price <= 0:
        try:
            service = get_polymarket_service()
            prices = await service.get_market_prices(market_id)
            resolved_price = _to_float(prices.get("yes_price"))
        except Exception as e:
            logger.warning(
                "Kelly size suggestion: price fetch failed market=%s: %s",
                market_id,
                e,
            )
            resolved_price = 0.0

    if not (0.0 < resolved_price < 1.0):
        return SizeSuggestionResponse(
            suggested_size_usdc=0.0,
            edge_probability=p_market,
            kelly_fraction=multiplier,
            capped_by="price_unavailable",
            edge_source=edge_source,
            bankroll_usdc=None,
            kelly_size_usdc=None,
        )

    p, kelly_price = kelly_service.kelly_trade_params(p_market, edge_source, side, resolved_price)

    # Bankroll = USDC balance + unrealized PnL (portfolio equity calc).
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError:
        stored = None
    pk = stored["private_key"] if stored else None
    creds = stored.get("clob_creds") if stored else None

    bankroll = await kelly_service.compute_bankroll(
        get_polymarket_service(),
        wallet_address,
        private_key=pk,
        clob_creds=creds,
    )

    max_pos = _to_float(settings.max_position_size)
    kelly_size = kelly_service.size(
        bankroll,
        p,
        kelly_price,
        multiplier,
        max_position_size=max_pos,
    )

    # Apply the same global safety caps the execute endpoint runs,
    # so the suggestion never exceeds the remaining loss budget or
    # position allowance.
    final_size, rejection = apply_global_safety_caps(settings, kelly_size, db)
    if rejection:
        commit_trading_settings(db)
        logger.info(
            "Kelly size suggestion: rejected by safety caps user=%d market=%s reason=%s",
            user_id,
            market_id,
            rejection,
        )
        return SizeSuggestionResponse(
            suggested_size_usdc=0.0,
            edge_probability=p_market,
            kelly_fraction=multiplier,
            capped_by=rejection,
            edge_source=edge_source,
            bankroll_usdc=bankroll,
            kelly_size_usdc=kelly_size,
        )

    # Report what capped the raw Kelly size.
    capped_by: str | None = None
    if kelly_size <= 0:
        capped_by = "no_edge" if p_market is None else "balance"
    elif max_pos > 0 and kelly_size >= max_pos - 0.005:
        capped_by = "max_position_size"
    elif kelly_size <= kelly_service.MIN_ORDER_SIZE_USDC + 0.005:
        capped_by = "min_order"
    elif final_size < kelly_size - 0.005:
        capped_by = "safety_caps"

    logger.info(
        "Kelly size suggestion: user=%d market=%s side=%s edge_source=%s "
        "p=%.4f price=%.4f bankroll=%.2f kelly_size=%.2f final=%.2f "
        "capped_by=%s",
        user_id,
        market_id,
        side,
        edge_source,
        p,
        kelly_price,
        bankroll,
        kelly_size,
        final_size,
        capped_by,
    )

    return SizeSuggestionResponse(
        suggested_size_usdc=final_size,
        edge_probability=p_market,
        kelly_fraction=multiplier,
        capped_by=capped_by,
        edge_source=edge_source,
        bankroll_usdc=bankroll,
        kelly_size_usdc=kelly_size,
    )


# ────────────── Per-Trader Copy Attribution (plan 07) ──────────────


class CopyAttributionTrader(BaseModel):
    """Realized copy-PnL attribution for one copied trader."""

    wallet: str
    label: str
    realized_pnl: float = 0.0
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    volume_usdc: float = 0.0
    open_positions: int = 0
    last_trade_at: str | None = None


class CopyAttributionTotals(BaseModel):
    """Copy attribution summed across every trader in the window."""

    realized_pnl: float = 0.0
    trade_count: int = 0
    win_rate: float | None = None


class CopyAttributionResponse(BaseModel):
    """Per-trader realized PnL for copy trades over a rolling window."""

    window_days: int
    generated_at: str
    traders: list[CopyAttributionTrader] = []
    totals: CopyAttributionTotals
    data_quality: dict[str, int] = {}


@router.get("/copy-trades/pnl/by-trader", response_model=CopyAttributionResponse)
async def get_copy_trade_attribution(
    window_days: int = Query(default=30, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Per-trader realized PnL and win rate for copy trades.

    Unlike ``GET /copy-trades/pnl`` (a single today-only number), this
    splits the PnL by ``copied_from_wallet`` over a rolling window, so
    "which trader actually made me money?" is answerable per row.

    ``UserTrade.pnl`` is written only by the PnL reconciler
    (``app.services.analytics.pnl_reconciliation``) — this endpoint reads it. Sells
    that reached the exchange but were never reconciled are excluded from
    the PnL sums and reported in ``data_quality.unreconciled_sells``, and
    copy trades whose ``executed_at`` was never recorded (so they match no
    window at all) are reported in ``data_quality.missing_executed_at``, so
    attribution never silently over- or under-states a trader.
    """
    from app.services.trading.copy_trade_service import get_trader_copy_stats

    # Subscript, not .get(): a token payload without ``user_id`` is a broken
    # auth contract and must fail loudly rather than quietly returning an
    # empty attribution table that reads as "no copy trades".
    user_id = current_user["user_id"]
    stats = get_trader_copy_stats(db, user_id, window_days=window_days)
    return CopyAttributionResponse(**stats)
