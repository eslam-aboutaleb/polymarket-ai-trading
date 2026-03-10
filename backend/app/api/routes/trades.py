"""Trade History, Leaderboard, Copy-Trading & Trade Execution API routes."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from sqlalchemy.orm import Session
import logging
import json

from app.api.routes.auth import get_current_user_from_token, get_optional_user_from_token
from app.services.polymarket_service import (
    PolymarketService,
    get_polymarket_service,
)
from app.services.leaderboard_service import fetch_leaderboard, fetch_trader_profile, fetch_trader_trades
from app.services.copy_trade_service import (
    get_copy_trade_history,
    get_copy_trade_evaluation,
    get_daily_copy_pnl,
    _place_order_on_polymarket,
)
from app.models.followed_trader import FollowedTrader
from app.models.notification_followed_trader import NotificationFollowedTrader
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.user_trade import UserTrade
from app.models.stop_loss import StopLossOrder
from app.models.take_profit import TakeProfitOrder
from app.models.user_settings import UserSettings
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.utils.database import get_db
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _safe_json_obj(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


# ────────────── Schemas ──────────────

class TradeRecord(BaseModel):
    id: Optional[str] = None
    market: Optional[str] = None
    market_slug: Optional[str] = None
    condition_id: Optional[str] = None
    outcome: Optional[str] = None
    side: Optional[str] = None
    size: float = 0.0
    price: float = 0.0
    type: Optional[str] = None
    status: Optional[str] = None
    timestamp: Optional[str] = None
    fee: float = 0.0
    fee_rate_bps: Optional[float] = None
    pnl: Optional[float] = None
    transaction_hash: Optional[str] = None
    maker_address: Optional[str] = None
    trader_side: Optional[str] = None


class TradeHistoryResponse(BaseModel):
    wallet_address: str
    trades: List[TradeRecord] = []
    count: int = 0
    has_more: bool = False


class LeaderboardEntry(BaseModel):
    rank: int
    address: str
    display_name: Optional[str] = None
    profit_loss: float = 0.0
    volume: float = 0.0
    markets_traded: int = 0
    win_rate: Optional[float] = None
    positions_value: float = 0.0
    pnl_24h: float = 0.0
    pnl_7d: float = 0.0
    pnl_30d: float = 0.0
    volume_24h: float = 0.0
    profile_image: Optional[str] = None
    is_followed: bool = False
    is_notification_followed: bool = False
    quality_score: Optional[float] = None
    quality_tier: Optional[str] = None


class LeaderboardResponse(BaseModel):
    entries: List[LeaderboardEntry] = []
    period: str = "all_time"
    updated_at: str
    total: int = 0


class TraderProfileResponse(BaseModel):
    wallet_address: str
    display_name: Optional[str] = None
    profit_loss: float = 0.0
    volume: float = 0.0
    markets_traded: int = 0
    win_rate: Optional[float] = None
    positions: List[dict] = []
    recent_trades: List[dict] = []
    profile_image: Optional[str] = None
    trade_stats: Optional[dict] = None  # Real stats computed from on-chain data


class FollowTraderRequest(BaseModel):
    max_position_size: Optional[float] = Field(default=None, gt=0)
    trader_alias: Optional[str] = Field(default=None, max_length=100)
    sizing_mode: Optional[str] = Field(
        default=None,
        pattern="^(inherit_global|fixed_amount|trader_wallet_ratio)$",
    )
    fixed_trade_amount_override: Optional[float] = Field(default=None, gt=0)
    copy_wallet_mode: Optional[str] = Field(
        default=None,
        pattern="^(dynamic_main_wallet_percentage|fixed_snapshot_amount)$",
    )
    copy_wallet_percentage: Optional[float] = Field(default=None, gt=0, le=100)
    copy_wallet_fixed_amount: Optional[float] = Field(default=None, gt=0)


class FollowedTraderResponse(BaseModel):
    id: int
    trader_wallet: str
    trader_alias: Optional[str] = None
    display_name: Optional[str] = None
    is_active: bool = True
    max_position_size: Optional[float] = None
    sizing_mode: str = "inherit_global"
    fixed_trade_amount_override: Optional[float] = None
    copy_wallet_mode: str = "dynamic_main_wallet_percentage"
    copy_wallet_percentage: float = 100.0
    copy_wallet_fixed_amount: Optional[float] = None
    created_at: str


class NotificationFollowRequest(BaseModel):
    feed_enabled: Optional[bool] = None
    email_enabled: Optional[bool] = None


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
    source_trade_history_id: Optional[int] = None
    email_status: str
    email_error: Optional[str] = None
    emailed_at: Optional[str] = None
    created_at: str


class CopyTradeRecord(BaseModel):
    id: int
    trader_wallet: str
    market_id: str
    side: str
    size: float
    price: float
    status: str
    pnl: Optional[float] = None
    timestamp: str
    source_trade_history_id: Optional[int] = None
    trader_trade_notional: Optional[float] = None
    trader_wallet_balance: Optional[float] = None
    copy_wallet_base: Optional[float] = None
    sizing_mode_applied: Optional[str] = None
    copy_wallet_mode_applied: Optional[str] = None
    calculation_warning: Optional[str] = None
    calculation_details: Optional[dict] = None


class CopyEvaluationRowResponse(BaseModel):
    source_trade_id: int
    source_trade_id_ext: Optional[str] = None
    source_timestamp: Optional[str] = None
    market_id: str
    side: str
    price: float
    source_trade_notional: float
    copy_trade_id: Optional[int] = None
    copy_timestamp: Optional[str] = None
    copied_size: Optional[float] = None
    copy_status: str = "not_copied"
    order_hash: Optional[str] = None
    trader_wallet_balance: Optional[float] = None
    copy_wallet_base: Optional[float] = None
    ratio: Optional[float] = None
    sizing_mode_applied: Optional[str] = None
    copy_wallet_mode_applied: Optional[str] = None
    warning: Optional[str] = None


class CopyEvaluationResponse(BaseModel):
    wallet: str
    rows: List[CopyEvaluationRowResponse]
    count: int
    updated_at: str


class ExecuteTradeRequest(BaseModel):
    """Request to execute a Buy/Sell on Polymarket."""
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
    order_hash: Optional[str] = None
    trade_id: Optional[int] = None
    status: str
    side: str
    size: float
    price: float
    error: Optional[str] = None


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
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Execute a Buy or Sell limit order on Polymarket via py-clob-client.
    Requires the user to have logged in with a private key.
    """
    wallet_address = current_user["wallet_address"]
    user_id = current_user.get("user_id")

    # Get stored credentials
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Credential storage is unavailable: {exc}",
        )
    if not stored:
        raise HTTPException(
            status_code=400,
            detail="No trading credentials found. Please re-login with your private key.",
        )

    pk = stored["private_key"]
    creds = stored.get("clob_creds")

    logger.info(
        "Executing %s order: token=%s price=%.4f size=%.4f market=%s",
        body.side, body.token_id[:20], body.price, body.size,
        (body.market_id or "")[:20],
    )

    # Place order
    result = _place_order_on_polymarket(
        private_key=pk,
        clob_creds=creds,
        token_id=body.token_id,
        side=body.side,
        price=body.price,
        size=body.size,
    )

    logger.info("Order result: success=%s hash=%s error=%s",
                result.get("success"), result.get("order_hash"), result.get("error"))

    # Record in user_trades table
    trade_id = None
    try:
        status = "executed" if result["success"] else "failed"
        user_trade = UserTrade(
            user_id=user_id,
            market_id=body.market_id or body.token_id,
            action=body.side.lower(),
            amount=body.size,
            price=body.price,
            status=status,
            order_hash=result.get("order_hash"),
            executed_at=utc_now() if result["success"] else None,
        )
        db.add(user_trade)
        db.commit()
        db.refresh(user_trade)
        trade_id = user_trade.id
    except Exception as e:
        logger.error("Failed to record trade: %s", e)
        db.rollback()

    if not result["success"]:
        return ExecuteTradeResponse(
            success=False,
            status="failed",
            side=body.side,
            size=body.size,
            price=body.price,
            error=result.get("error", "Order placement failed"),
            trade_id=trade_id,
        )

    return ExecuteTradeResponse(
        success=True,
        order_hash=result.get("order_hash"),
        trade_id=trade_id,
        status="submitted",
        side=body.side,
        size=body.size,
        price=body.price,
    )


# ────────────── Cash-Out (Sell Position) ──────────────

@router.post("/cash-out")
async def cash_out_position(
    body: ExecuteTradeRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Cash out (sell) an existing position at a specified price.
    This is a convenience wrapper around the execute endpoint
    that always creates a SELL order.
    """
    body.side = "SELL"
    return await execute_trade(body, current_user, db)


# ────────────── Leaderboard ──────────────

@router.get("/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    limit: int = Query(default=25, le=1000),
    period: str = Query(default="all_time", pattern="^(24h|7d|30d|all_time)$"),
    current_user: Optional[dict] = Depends(get_optional_user_from_token),
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
        followed = db.query(FollowedTrader.trader_wallet).filter(
            FollowedTrader.user_id == user_id,
            FollowedTrader.is_active == True,
        ).all()
        followed_wallets = {f.trader_wallet.lower() for f in followed}
        notification_followed = (
            db.query(NotificationFollowedTrader.trader_wallet)
            .filter(
                NotificationFollowedTrader.user_id == user_id,
                NotificationFollowedTrader.is_active == True,
            )
            .all()
        )
        notification_followed_wallets = {
            f.trader_wallet.lower() for f in notification_followed
        }

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
                is_notification_followed=(
                    addr.lower() in notification_followed_wallets
                ),
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
    real_trade_count = trade_stats.get("total_trades", 0)

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


def _wallet_has_any_active_watchers(
    db: Session,
    wallet: str,
) -> bool:
    normalized = wallet.lower()
    copy_q = db.query(FollowedTrader).filter(
        FollowedTrader.trader_wallet == normalized,
        FollowedTrader.is_active == True,
    )
    notif_q = db.query(NotificationFollowedTrader).filter(
        NotificationFollowedTrader.trader_wallet == normalized,
        NotificationFollowedTrader.is_active == True,
    )
    return copy_q.first() is not None or notif_q.first() is not None


@router.post("/follow/{wallet}", response_model=FollowedTraderResponse)
async def follow_trader(
    wallet: str,
    body: FollowTraderRequest = FollowTraderRequest(),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Start following a trader for copy-trading."""
    user_id = current_user.get("user_id")
    provided_fields = body.model_fields_set

    normalized_alias: Optional[str] = None
    if "trader_alias" in provided_fields:
        if body.trader_alias is not None:
            trimmed_alias = body.trader_alias.strip()
            normalized_alias = trimmed_alias if trimmed_alias else None
        else:
            normalized_alias = None

    existing = db.query(FollowedTrader).filter(
        FollowedTrader.user_id == user_id,
        FollowedTrader.trader_wallet == wallet.lower(),
    ).first()

    if existing:
        existing.is_active = True
        if "max_position_size" in provided_fields:
            existing.max_position_size = body.max_position_size
        if "trader_alias" in provided_fields:
            existing.trader_alias = normalized_alias
        if "sizing_mode" in provided_fields:
            existing.sizing_mode = body.sizing_mode or "inherit_global"
        if "fixed_trade_amount_override" in provided_fields:
            existing.fixed_trade_amount_override = body.fixed_trade_amount_override
        if "copy_wallet_mode" in provided_fields:
            existing.copy_wallet_mode = body.copy_wallet_mode or "dynamic_main_wallet_percentage"
        if "copy_wallet_percentage" in provided_fields:
            existing.copy_wallet_percentage = body.copy_wallet_percentage or 100.0
        if "copy_wallet_fixed_amount" in provided_fields:
            existing.copy_wallet_fixed_amount = body.copy_wallet_fixed_amount
        existing.updated_at = utc_now()
        db.commit()
        db.refresh(existing)
        record = existing
    else:
        record = FollowedTrader(
            user_id=user_id,
            trader_wallet=wallet.lower(),
            is_active=True,
            max_position_size=body.max_position_size,
            trader_alias=normalized_alias if "trader_alias" in provided_fields else None,
            sizing_mode=body.sizing_mode or "inherit_global",
            fixed_trade_amount_override=body.fixed_trade_amount_override,
            copy_wallet_mode=body.copy_wallet_mode or "dynamic_main_wallet_percentage",
            copy_wallet_percentage=body.copy_wallet_percentage or 100.0,
            copy_wallet_fixed_amount=body.copy_wallet_fixed_amount,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

    # Dynamically add wallet to trade monitor
    try:
        from app.services.trade_monitor import add_watched_wallet
        add_watched_wallet(wallet.lower())
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

    record = db.query(FollowedTrader).filter(
        FollowedTrader.user_id == user_id,
        FollowedTrader.trader_wallet == wallet.lower(),
    ).first()

    if not record:
        raise HTTPException(status_code=404, detail="Not following this trader")

    record.is_active = False
    record.updated_at = utc_now()
    db.commit()

    # Remove from monitor only if no active copy or notification subscribers remain.
    if not _wallet_has_any_active_watchers(db, wallet):
        try:
            from app.services.trade_monitor import remove_watched_wallet
            remove_watched_wallet(wallet.lower())
        except Exception as e:
            logger.warning(f"Could not remove wallet from monitor: {e}")

    return {"status": "unfollowed", "wallet": wallet}


@router.get("/following", response_model=List[FollowedTraderResponse])
async def get_following(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get list of traders the user is currently following."""
    user_id = current_user.get("user_id")

    records = db.query(FollowedTrader).filter(
        FollowedTrader.user_id == user_id,
        FollowedTrader.is_active == True,
    ).order_by(FollowedTrader.created_at.desc()).all()

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
    normalized_wallet = wallet.lower()
    provided_fields = body.model_fields_set

    record = db.query(NotificationFollowedTrader).filter(
        NotificationFollowedTrader.user_id == user_id,
        NotificationFollowedTrader.trader_wallet == normalized_wallet,
    ).first()

    if record:
        record.is_active = True
        if "feed_enabled" in provided_fields and body.feed_enabled is not None:
            record.feed_enabled = bool(body.feed_enabled)
        if "email_enabled" in provided_fields and body.email_enabled is not None:
            record.email_enabled = bool(body.email_enabled)
        if not record.feed_enabled and not record.email_enabled:
            record.feed_enabled = True
        record.updated_at = utc_now()
        db.commit()
        db.refresh(record)
    else:
        record = NotificationFollowedTrader(
            user_id=user_id,
            trader_wallet=normalized_wallet,
            is_active=True,
            feed_enabled=(
                bool(body.feed_enabled)
                if body.feed_enabled is not None
                else True
            ),
            email_enabled=(
                bool(body.email_enabled)
                if body.email_enabled is not None
                else False
            ),
        )
        if not record.feed_enabled and not record.email_enabled:
            record.feed_enabled = True
        db.add(record)
        db.commit()
        db.refresh(record)

    try:
        from app.services.trade_monitor import add_watched_wallet
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
    record = db.query(NotificationFollowedTrader).filter(
        NotificationFollowedTrader.user_id == user_id,
        NotificationFollowedTrader.trader_wallet == normalized_wallet,
    ).first()

    if not record:
        raise HTTPException(
            status_code=404,
            detail="Not following this trader for notifications",
        )

    record.is_active = False
    record.feed_enabled = False
    record.email_enabled = False
    record.updated_at = utc_now()
    db.commit()

    if not _wallet_has_any_active_watchers(db, normalized_wallet):
        try:
            from app.services.trade_monitor import remove_watched_wallet
            remove_watched_wallet(normalized_wallet)
        except Exception as e:
            logger.warning(f"Could not remove notification wallet from monitor: {e}")

    return {"status": "notification_unfollowed", "wallet": normalized_wallet}


@router.get(
    "/notification-following",
    response_model=List[NotificationFollowedTraderResponse],
)
async def get_notification_following(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List active notification-followed traders for the current user."""
    user_id = current_user.get("user_id")
    rows = (
        db.query(NotificationFollowedTrader)
        .filter(
            NotificationFollowedTrader.user_id == user_id,
            NotificationFollowedTrader.is_active == True,
        )
        .order_by(NotificationFollowedTrader.created_at.desc())
        .all()
    )
    return [_notification_followed_to_response(r) for r in rows]


@router.get("/following-feed", response_model=List[FollowingFeedEventResponse])
async def get_following_feed(
    limit: int = Query(default=50, le=200),
    wallet: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None, pattern="^(opened|closed)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get chronological followed-trader opened/closed events for dashboard feed."""
    user_id = current_user.get("user_id")
    q = db.query(NotificationFeedEvent).filter(
        NotificationFeedEvent.user_id == user_id,
    )
    if wallet:
        q = q.filter(NotificationFeedEvent.trader_wallet == wallet.lower())
    if event_type:
        q = q.filter(NotificationFeedEvent.event_type == event_type)
    rows = q.order_by(NotificationFeedEvent.created_at.desc()).limit(limit).all()

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

@router.get("/copy-trades", response_model=List[CopyTradeRecord])
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
            id=t["id"],
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
                float(t["copy_wallet_base"])
                if t.get("copy_wallet_base") is not None
                else None
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
    order_hash: Optional[str] = None
    executed_price: Optional[float] = None
    triggered_at: Optional[str] = None
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

    # Check for existing active stop-loss on same token
    existing = db.query(StopLossOrder).filter(
        StopLossOrder.user_id == user_id,
        StopLossOrder.token_id == body.token_id,
        StopLossOrder.status == "active",
    ).first()

    if existing:
        existing.stop_price = body.stop_price
        existing.size = body.size
        existing.market_title = body.market_title or existing.market_title
        existing.outcome = body.outcome or existing.outcome
        existing.updated_at = utc_now()
        db.commit()
        db.refresh(existing)
        return _sl_to_response(existing)

    sl = StopLossOrder(
        user_id=user_id,
        token_id=body.token_id,
        market_id=body.market_id,
        market_title=body.market_title,
        outcome=body.outcome,
        size=body.size,
        stop_price=body.stop_price,
        status="active",
    )
    db.add(sl)
    db.commit()
    db.refresh(sl)
    return _sl_to_response(sl)


@router.get("/stop-loss", response_model=List[StopLossResponse])
async def get_stop_losses(
    status: str = Query(default="active", pattern="^(active|triggered|cancelled|failed|all)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List stop-loss orders for the current user."""
    user_id = current_user.get("user_id")
    q = db.query(StopLossOrder).filter(StopLossOrder.user_id == user_id)
    if status != "all":
        q = q.filter(StopLossOrder.status == status)
    orders = q.order_by(StopLossOrder.created_at.desc()).all()
    return [_sl_to_response(o) for o in orders]


@router.delete("/stop-loss/{stop_loss_id}")
async def cancel_stop_loss(
    stop_loss_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Cancel an active stop-loss order."""
    user_id = current_user.get("user_id")
    sl = db.query(StopLossOrder).filter(
        StopLossOrder.id == stop_loss_id,
        StopLossOrder.user_id == user_id,
    ).first()

    if not sl:
        raise HTTPException(status_code=404, detail="Stop-loss order not found")
    if sl.status != "active":
        raise HTTPException(status_code=400, detail=f"Cannot cancel – status is '{sl.status}'")

    sl.status = "cancelled"
    sl.updated_at = utc_now()
    db.commit()
    return {"status": "cancelled", "id": stop_loss_id}


# ────────────── Take-Profit Orders ──────────────

class SetTakeProfitRequest(BaseModel):
    """Create or update a take-profit order."""
    token_id: str = Field(..., description="CLOB token ID")
    market_id: str = Field(default="", description="Market condition ID")
    market_title: str = Field(default="", description="Human-readable title")
    outcome: str = Field(default="", description="Yes/No outcome label")
    size: float = Field(..., gt=0, description="Number of shares")
    take_profit_price: float = Field(..., gt=0, lt=1, description="Trigger price (0-1) – sells when price >= this")


class TakeProfitResponse(BaseModel):
    id: int
    token_id: str
    market_id: str
    market_title: str
    outcome: str
    size: float
    take_profit_price: float
    status: str
    order_hash: Optional[str] = None
    executed_price: Optional[float] = None
    triggered_at: Optional[str] = None
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

    existing = db.query(TakeProfitOrder).filter(
        TakeProfitOrder.user_id == user_id,
        TakeProfitOrder.token_id == body.token_id,
        TakeProfitOrder.status == "active",
    ).first()

    if existing:
        existing.take_profit_price = body.take_profit_price
        existing.size = body.size
        existing.market_title = body.market_title or existing.market_title
        existing.outcome = body.outcome or existing.outcome
        existing.updated_at = utc_now()
        db.commit()
        db.refresh(existing)
        return _tp_to_response(existing)

    tp = TakeProfitOrder(
        user_id=user_id,
        token_id=body.token_id,
        market_id=body.market_id,
        market_title=body.market_title,
        outcome=body.outcome,
        size=body.size,
        take_profit_price=body.take_profit_price,
        status="active",
    )
    db.add(tp)
    db.commit()
    db.refresh(tp)
    return _tp_to_response(tp)


@router.get("/take-profit", response_model=List[TakeProfitResponse])
async def get_take_profits(
    status: str = Query(default="active", pattern="^(active|triggered|cancelled|failed|all)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List take-profit orders for the current user."""
    user_id = current_user.get("user_id")
    q = db.query(TakeProfitOrder).filter(TakeProfitOrder.user_id == user_id)
    if status != "all":
        q = q.filter(TakeProfitOrder.status == status)
    orders = q.order_by(TakeProfitOrder.created_at.desc()).all()
    return [_tp_to_response(o) for o in orders]


@router.delete("/take-profit/{take_profit_id}")
async def cancel_take_profit(
    take_profit_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Cancel an active take-profit order."""
    user_id = current_user.get("user_id")
    tp = db.query(TakeProfitOrder).filter(
        TakeProfitOrder.id == take_profit_id,
        TakeProfitOrder.user_id == user_id,
    ).first()

    if not tp:
        raise HTTPException(status_code=404, detail="Take-profit order not found")
    if tp.status != "active":
        raise HTTPException(status_code=400, detail=f"Cannot cancel – status is '{tp.status}'")

    tp.status = "cancelled"
    tp.updated_at = utc_now()
    db.commit()
    return {"status": "cancelled", "id": take_profit_id}


# ────────────── Emergency Stop / Panic Sell ──────────────

class EmergencyStopResponse(BaseModel):
    halted: bool
    positions_closed: int = 0
    stop_losses_cancelled: int = 0
    take_profits_cancelled: int = 0
    errors: List[str] = []
    message: str = ""


@router.post("/emergency-stop", response_model=EmergencyStopResponse)
async def emergency_stop(
    close_positions: bool = Query(default=True, description="Also attempt to sell all open positions"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Emergency stop: immediately halt all copy-trading and optionally
    close every open position (panic sell at current market prices).

    Steps:
      1. Disable copy-trading and set trading_halted flag.
      2. Cancel all active stop-loss and take-profit orders.
      3. If close_positions=true, sell every open position at market price.
    """
    user_id = current_user.get("user_id")
    errors: List[str] = []

    # 1 – Halt trading
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if settings:
        settings.copy_trading_enabled = False
        settings.trading_halted = True
        settings.halt_reason = "Emergency stop triggered by user"
        settings.updated_at = utc_now()
    else:
        errors.append("User settings not found; created halt record")
        settings = UserSettings(
            user_id=user_id,
            copy_trading_enabled=False,
            trading_halted=True,
            halt_reason="Emergency stop triggered by user",
        )
        db.add(settings)

    # 2 – Cancel all active SL/TP orders
    sl_cancelled = (
        db.query(StopLossOrder)
        .filter(StopLossOrder.user_id == user_id, StopLossOrder.status == "active")
        .update({"status": "cancelled", "updated_at": utc_now()})
    )
    tp_cancelled = (
        db.query(TakeProfitOrder)
        .filter(TakeProfitOrder.user_id == user_id, TakeProfitOrder.status == "active")
        .update({"status": "cancelled", "updated_at": utc_now()})
    )

    db.commit()

    positions_closed = 0

    # 3 – Panic sell all positions
    if close_positions:
        from app.models.user import User
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            errors.append("User not found – cannot close positions")
        else:
            try:
                stored = load_wallet_credentials(user.wallet_address)
            except CredentialStoreError:
                stored = None

            if not stored:
                errors.append("No wallet credentials – cannot close positions")
            else:
                pk = stored["private_key"]
                creds = stored.get("clob_creds")
                service = get_polymarket_service()
                try:
                    positions = await service.get_positions(
                        user.wallet_address,
                        private_key=pk,
                        clob_creds=creds,
                    )
                except Exception as e:
                    positions = []
                    errors.append(f"Failed to fetch positions: {e}")

                for pos in positions:
                    size = float(pos.get("size", 0))
                    if size <= 0:
                        continue
                    token_id = pos.get("asset") or pos.get("token_id") or pos.get("tokenId", "")
                    if not token_id:
                        continue
                    cur_price = float(pos.get("curPrice", pos.get("price", 0.5)))
                    # Sell at slightly below market to ensure fill
                    sell_price = max(0.01, round(cur_price * 0.98, 4))

                    try:
                        result = _place_order_on_polymarket(
                            private_key=pk,
                            clob_creds=creds,
                            token_id=token_id,
                            side="SELL",
                            price=sell_price,
                            size=size,
                        )
                        if result.get("success"):
                            positions_closed += 1
                        else:
                            errors.append(
                                f"Failed to sell {token_id[:12]}…: {result.get('error', 'unknown')}"
                            )
                    except Exception as e:
                        errors.append(f"Exception selling {token_id[:12]}…: {e}")

    return EmergencyStopResponse(
        halted=True,
        positions_closed=positions_closed,
        stop_losses_cancelled=sl_cancelled,
        take_profits_cancelled=tp_cancelled,
        errors=errors,
        message=(
            f"Emergency stop complete. Trading halted, "
            f"{sl_cancelled} SL + {tp_cancelled} TP orders cancelled, "
            f"{positions_closed} positions closed."
        ),
    )


@router.post("/resume-trading")
async def resume_trading(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Resume trading after an emergency stop or risk halt.
    Clears the trading_halted flag and cooldown.
    """
    user_id = current_user.get("user_id")
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        raise HTTPException(status_code=404, detail="User settings not found")

    settings.trading_halted = False
    settings.halt_reason = None
    settings.cooldown_until = None
    settings.updated_at = utc_now()
    db.commit()

    return {"resumed": True, "message": "Trading resumed successfully"}


# ────────────── Trader Quality Scoring ──────────────

class TraderQualityResponse(BaseModel):
    wallet_address: str
    quality_score: Optional[float] = None
    consistency_score: Optional[float] = None
    risk_adjusted_score: Optional[float] = None
    activity_score: Optional[float] = None
    win_rate_score: Optional[float] = None
    quality_tier: Optional[str] = None


@router.get("/trader/{wallet}/quality", response_model=TraderQualityResponse)
async def get_trader_quality_score(
    wallet: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get quality score breakdown for a trader."""
    from app.services.trader_quality_service import get_trader_quality
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
    from app.services.trader_quality_service import score_all_traders
    count = score_all_traders(db)
    return {"scored": count, "message": f"Quality scores updated for {count} traders"}


# ────────────── Arbitrage Detection ──────────────

class ArbitrageOpportunity(BaseModel):
    type: str
    market_id: str
    market_title: str
    profit_pct: Optional[float] = None
    spread_pct: Optional[float] = None
    total_cost: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    token_id: Optional[str] = None
    outcome: Optional[str] = None
    detected_at: str


@router.get("/arbitrage/opportunities", response_model=List[ArbitrageOpportunity])
async def get_arbitrage_opportunities(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get recently detected arbitrage opportunities."""
    from app.services.arbitrage_service import get_recent_opportunities
    return get_recent_opportunities()


@router.post("/arbitrage/scan")
async def trigger_arbitrage_scan(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Trigger an immediate arbitrage scan."""
    from app.services.arbitrage_service import scan_for_arbitrage
    opportunities = await scan_for_arbitrage()
    return {
        "found": len(opportunities),
        "opportunities": opportunities[:20],
    }
