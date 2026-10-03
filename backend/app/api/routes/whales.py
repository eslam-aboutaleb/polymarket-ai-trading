"""Whale monitoring API — on-chain whale activity feed and per-user config."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.models.whale_config import WhaleConfig
from app.models.whale_event import WhaleEvent
from app.services.ctf_events_service import (
    WHALE_AUTO_COPY,
    WHALE_MIN_NOTIONAL,
    WHALE_SET_SIZE,
    get_user_config,
    invalidate_config_cache,
)
from app.utils.database import get_db

router = APIRouter(prefix="/api/whales", tags=["whales"])


class WhaleEventResponse(BaseModel):
    id: int
    wallet: str
    market_id: str
    token_id: str
    event_type: str
    side: str
    size: float
    price: float
    notional: float
    tx_hash: str
    log_index: int | None = None
    block_number: int | None = None
    block_ts: datetime | None = None
    detected_at: datetime

    model_config = {"from_attributes": True}


class WhaleEventsResponse(BaseModel):
    events: list[WhaleEventResponse]
    total: int
    limit: int
    offset: int


class WhaleConfigResponse(BaseModel):
    min_notional: float
    auto_copy: bool
    watchlist: list[str]
    whale_set_size: int


class WhaleConfigUpdate(BaseModel):
    min_notional: float | None = Field(None, gt=0)
    auto_copy: bool | None = None
    watchlist: list[str] | None = None
    whale_set_size: int | None = Field(None, ge=1, le=500)


def _event_to_response(event: WhaleEvent) -> WhaleEventResponse:
    return WhaleEventResponse(
        id=event.id,
        wallet=event.wallet,
        market_id=event.market_id,
        token_id=event.token_id,
        event_type=event.event_type,
        side=event.side,
        size=event.size,
        price=event.price,
        notional=event.notional,
        tx_hash=event.tx_hash,
        log_index=event.log_index,
        block_number=event.block_number,
        block_ts=event.block_ts,
        detected_at=event.detected_at,
    )


@router.get("/events", response_model=WhaleEventsResponse)
async def list_whale_events(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    wallet: str | None = Query(None),
    market_id: str | None = Query(None),
    side: str | None = Query(None),
    event_type: str | None = Query(None),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Paginated on-chain whale activity feed (newest first)."""
    query = db.query(WhaleEvent)
    if wallet:
        query = query.filter(WhaleEvent.wallet == wallet.strip().lower())
    if market_id:
        query = query.filter(WhaleEvent.market_id == market_id)
    if side:
        query = query.filter(WhaleEvent.side == side)
    if event_type:
        query = query.filter(WhaleEvent.event_type == event_type)

    total = query.count()
    rows = query.order_by(WhaleEvent.detected_at.desc()).offset(offset).limit(limit).all()
    return WhaleEventsResponse(
        events=[_event_to_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/config", response_model=WhaleConfigResponse)
async def get_whale_config(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Current user's whale monitoring thresholds."""
    config = get_user_config(int(current_user["user_id"]), db)
    return WhaleConfigResponse(
        min_notional=config.min_notional,
        auto_copy=config.auto_copy,
        watchlist=list(config.watchlist or []),
        whale_set_size=config.whale_set_size,
    )


@router.put("/config", response_model=WhaleConfigResponse)
async def update_whale_config(
    body: WhaleConfigUpdate,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Update whale monitoring thresholds, watchlist and auto-copy toggle."""
    user_id = int(current_user["user_id"])
    config = db.query(WhaleConfig).filter(WhaleConfig.user_id == user_id).first()
    if config is None:
        config = WhaleConfig(
            user_id=user_id,
            min_notional=WHALE_MIN_NOTIONAL,
            auto_copy=WHALE_AUTO_COPY,
            watchlist=[],
            whale_set_size=WHALE_SET_SIZE,
        )
        db.add(config)

    if body.min_notional is not None:
        config.min_notional = body.min_notional
    if body.auto_copy is not None:
        config.auto_copy = body.auto_copy and WHALE_AUTO_COPY
    if body.watchlist is not None:
        config.watchlist = [w.strip().lower() for w in body.watchlist if w.strip()]
    if body.whale_set_size is not None:
        config.whale_set_size = body.whale_set_size

    db.commit()
    db.refresh(config)
    invalidate_config_cache(user_id)
    return WhaleConfigResponse(
        min_notional=config.min_notional,
        auto_copy=config.auto_copy,
        watchlist=list(config.watchlist or []),
        whale_set_size=config.whale_set_size,
    )
