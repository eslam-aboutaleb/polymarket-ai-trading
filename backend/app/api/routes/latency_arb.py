"""Latency-arbitrage API — config, live edge board and trade log."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.models.user_trade import UserTrade
from app.services.latency_arb_service import (
    STRATEGY_SOURCE,
    get_config,
    get_engine_status,
    get_latest_opportunities,
    get_or_create_config,
    latency_stats,
)
from app.utils.database import get_db

router = APIRouter(prefix="/api/latency-arb", tags=["latency-arb"])


class LatencyArbConfigResponse(BaseModel):
    enabled: bool
    edge_threshold: float
    max_notional: float
    symbols: list[str]
    windows: list[int]
    late_entry: bool
    daily_loss_limit: float
    alert_on_opportunity: bool


class LatencyArbConfigUpdate(BaseModel):
    enabled: bool | None = None
    edge_threshold: float | None = Field(None, gt=0.0, lt=1.0)
    max_notional: float | None = Field(None, gt=0.0)
    symbols: list[str] | None = None
    windows: list[int] | None = None
    late_entry: bool | None = None
    daily_loss_limit: float | None = Field(None, ge=0.0)
    alert_on_opportunity: bool | None = None


class OpportunityResponse(BaseModel):
    symbol: str
    window_minutes: int
    window_start_epoch: int
    window_end_epoch: int
    side: str
    p_model: float
    p_market: float
    edge: float
    distance: float
    t_remaining: float
    sigma: float
    current_price: float
    window_open: float
    condition_id: str
    question: str
    token_ids: dict[str, str]
    prices: dict[str, float]
    feed_lag_ms: float | None = None
    detected_at: str
    seconds_remaining: int


class LatencyStatsResponse(BaseModel):
    samples: int
    feed_lag_p50_ms: float
    feed_lag_p95_ms: float
    total_p50_ms: float
    total_p95_ms: float


class EngineStatusResponse(BaseModel):
    running: bool
    live_mode: bool
    last_cycle_at: str | None = None
    cycle_seconds: int
    symbols: list[str]
    windows: list[int]


class OpportunitiesResponse(BaseModel):
    engine: EngineStatusResponse
    latency: LatencyStatsResponse
    opportunities: list[OpportunityResponse]


class TradeResponse(BaseModel):
    id: int
    market_id: str
    token_id: str | None = None
    action: str
    amount: float
    price: float
    status: str
    order_hash: str | None = None
    expected_price: float | None = None
    expected_size: float | None = None
    filled_price: float | None = None
    filled_size: float | None = None
    fee_paid: float | None = None
    slippage_bps: float | None = None
    latency_ms: float | None = None
    pnl: float | None = None
    strategy_source: str | None = None
    calculation_details: str | None = None
    executed_at: datetime | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class TradesResponse(BaseModel):
    trades: list[TradeResponse]
    total: int
    limit: int
    offset: int


def _config_to_response(config) -> LatencyArbConfigResponse:
    return LatencyArbConfigResponse(
        enabled=bool(config.enabled),
        edge_threshold=float(config.edge_threshold),
        max_notional=float(config.max_notional),
        symbols=list(config.symbols or []),
        windows=[int(item) for item in config.windows or []],
        late_entry=bool(config.late_entry),
        daily_loss_limit=float(config.daily_loss_limit),
        alert_on_opportunity=bool(config.alert_on_opportunity),
    )


def _opportunity_to_response(
    opportunity: dict,
    now: datetime,
) -> OpportunityResponse:
    window_end = int(opportunity.get("window_end_epoch") or 0)
    seconds_remaining = max(0, int(window_end - now.timestamp()))
    return OpportunityResponse(
        symbol=str(opportunity.get("symbol") or ""),
        window_minutes=int(opportunity.get("window_minutes") or 0),
        window_start_epoch=int(opportunity.get("window_start_epoch") or 0),
        window_end_epoch=window_end,
        side=str(opportunity.get("side") or ""),
        p_model=float(opportunity.get("p_model") or 0.0),
        p_market=float(opportunity.get("p_market") or 0.0),
        edge=float(opportunity.get("edge") or 0.0),
        distance=float(opportunity.get("distance") or 0.0),
        t_remaining=float(opportunity.get("t_remaining") or 0.0),
        sigma=float(opportunity.get("sigma") or 0.0),
        current_price=float(opportunity.get("current_price") or 0.0),
        window_open=float(opportunity.get("window_open") or 0.0),
        condition_id=str(opportunity.get("condition_id") or ""),
        question=str(opportunity.get("question") or ""),
        token_ids=dict(opportunity.get("token_ids") or {}),
        prices=dict(opportunity.get("prices") or {}),
        feed_lag_ms=opportunity.get("feed_lag_ms"),
        detected_at=str(opportunity.get("detected_at") or ""),
        seconds_remaining=seconds_remaining,
    )


@router.get("/config", response_model=LatencyArbConfigResponse)
async def get_latency_arb_config(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Current user's latency-arbitrage engine configuration."""
    config = get_config(int(current_user["user_id"]), db)
    return _config_to_response(config)


@router.post("/config", response_model=LatencyArbConfigResponse)
async def update_latency_arb_config(
    body: LatencyArbConfigUpdate,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Update latency-arbitrage thresholds and market selection."""
    from app.services.crypto_markets_service import (
        SYMBOLS,
        WINDOW_MINUTES,
    )

    user_id = int(current_user["user_id"])
    config = get_or_create_config(user_id, db)

    if body.enabled is not None:
        config.enabled = body.enabled
    if body.edge_threshold is not None:
        config.edge_threshold = body.edge_threshold
    if body.max_notional is not None:
        config.max_notional = body.max_notional
    if body.symbols is not None:
        config.symbols = [
            item.strip().upper() for item in body.symbols if item.strip().upper() in SYMBOLS
        ]
    if body.windows is not None:
        config.windows = [int(item) for item in body.windows if int(item) in WINDOW_MINUTES]
    if body.late_entry is not None:
        config.late_entry = body.late_entry
    if body.daily_loss_limit is not None:
        config.daily_loss_limit = body.daily_loss_limit
    if body.alert_on_opportunity is not None:
        config.alert_on_opportunity = body.alert_on_opportunity

    db.commit()
    db.refresh(config)
    return _config_to_response(config)


@router.get("/opportunities", response_model=OpportunitiesResponse)
async def get_latency_arb_opportunities(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Live edge board: opportunities from the last engine cycle."""
    opportunities = get_latest_opportunities()
    now = datetime.now()
    return OpportunitiesResponse(
        engine=get_engine_status(),
        latency=latency_stats(),
        opportunities=[_opportunity_to_response(opportunity, now) for opportunity in opportunities],
    )


@router.get("/trades", response_model=TradesResponse)
async def get_latency_arb_trades(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Paginated latency-arbitrage trade log for the current user."""
    user_id = int(current_user["user_id"])
    query = db.query(UserTrade).filter(
        UserTrade.user_id == user_id,
        UserTrade.strategy_source == STRATEGY_SOURCE,
    )
    total = query.count()
    rows = query.order_by(UserTrade.created_at.desc()).offset(offset).limit(limit).all()
    return TradesResponse(
        trades=[TradeResponse.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
