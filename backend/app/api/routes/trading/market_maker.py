"""Market Maker API routes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_active_account, require_plan
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.trading.market_maker_config import MarketMakerConfig
from app.services.trading.market_maker_service import (
    get_market_maker_config_for_user,
    get_running_maker_ids,
    list_user_market_maker_configs,
    set_market_maker_config_state,
    start_market_maker,
    stop_market_maker,
    trigger_single_sync,
)
from app.services.trading.market_maker_service import (
    upsert_market_maker_config as upsert_market_maker_config_service,
)
from app.services.users.entitlement_service import CAP_MARKET_MAKER
from app.utils.database import get_db

router = APIRouter(prefix="/api/market-maker", tags=["market-maker"])


# ── Pydantic schemas ──


class MarketMakerConfigRequest(BaseModel):
    condition_id: str = Field(..., min_length=2)
    token_id_yes: str = Field(..., min_length=2)
    token_id_no: str = Field(..., min_length=2)
    market_title: str = ""
    enabled: bool = False
    strategy: str = Field(default="bands", pattern="^(bands|amm)$")
    num_bands: int = Field(default=3, ge=1, le=20)
    min_spread: float = Field(default=0.02, ge=0.001, le=0.5)
    max_spread: float = Field(default=0.10, ge=0.002, le=0.5)
    band_order_size: float = Field(default=10.0, ge=1.0, le=10000.0)
    amm_liquidity: float = Field(default=1000.0, ge=10.0, le=1000000.0)
    max_collateral: float = Field(default=500.0, ge=10.0, le=1000000.0)
    sync_interval_seconds: int = Field(default=30, ge=10, le=3600)
    min_order_size: float = Field(default=1.0, ge=0.01, le=100.0)
    min_price: float = Field(default=0.01, ge=0.01, le=0.99)
    max_price: float = Field(default=0.99, ge=0.01, le=0.99)


class MarketMakerConfigResponse(BaseModel):
    id: int
    condition_id: str
    token_id_yes: str
    token_id_no: str
    market_title: str
    enabled: bool
    strategy: str
    num_bands: int
    min_spread: float
    max_spread: float
    band_order_size: float
    amm_liquidity: float
    max_collateral: float
    sync_interval_seconds: int
    min_order_size: float
    min_price: float
    max_price: float
    status: str
    last_sync_at: str | None
    last_error: str | None
    total_orders_placed: int
    total_orders_cancelled: int
    total_volume_usdc: float
    current_open_orders: int
    is_running: bool
    created_at: str
    updated_at: str | None


class MarketMakerSyncResponse(BaseModel):
    success: bool
    result: dict[str, Any] | None = None
    detail: str | None = None


class MarketMakerMetricsResponse(BaseModel):
    running_configs: list[int]
    total_running: int


# ── Helpers ──


def _to_response(row: MarketMakerConfig) -> MarketMakerConfigResponse:
    running_ids = get_running_maker_ids()
    return MarketMakerConfigResponse(
        id=row.id,
        condition_id=row.condition_id,
        token_id_yes=row.token_id_yes,
        token_id_no=row.token_id_no,
        market_title=row.market_title or "",
        enabled=bool(row.enabled),
        strategy=row.strategy or "bands",
        num_bands=row.num_bands,
        min_spread=row.min_spread,
        max_spread=row.max_spread,
        band_order_size=row.band_order_size,
        amm_liquidity=row.amm_liquidity,
        max_collateral=row.max_collateral,
        sync_interval_seconds=row.sync_interval_seconds,
        min_order_size=row.min_order_size,
        min_price=row.min_price,
        max_price=row.max_price,
        status=row.status or "idle",
        last_sync_at=row.last_sync_at.isoformat() if row.last_sync_at else None,
        last_error=row.last_error,
        total_orders_placed=row.total_orders_placed or 0,
        total_orders_cancelled=row.total_orders_cancelled or 0,
        total_volume_usdc=row.total_volume_usdc or 0.0,
        current_open_orders=row.current_open_orders or 0,
        is_running=row.id in running_ids,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


# ── Endpoints ──


@router.get("/configs", response_model=list[MarketMakerConfigResponse])
async def list_market_maker_configs(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List all market maker configs for the current user."""
    user_id = current_user.get("user_id")
    rows = list_user_market_maker_configs(db, user_id)
    return [_to_response(r) for r in rows]


@router.post(
    "/configs",
    response_model=MarketMakerConfigResponse,
    dependencies=[Depends(require_plan(CAP_MARKET_MAKER))],
)
async def upsert_market_maker_config(
    body: MarketMakerConfigRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Create or update a market maker config for a market."""
    user_id = current_user.get("user_id")

    row = upsert_market_maker_config_service(db, user_id, body.model_dump())
    return _to_response(row)


@router.delete("/configs/{config_id}")
async def delete_market_maker_config(
    config_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Disable and stop a market maker config."""
    user_id = current_user.get("user_id")
    row = get_market_maker_config_for_user(db, config_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    # Stop it if running
    await stop_market_maker(config_id)

    set_market_maker_config_state(db, row, enabled=False, status="idle")
    return {"status": "disabled", "id": config_id}


@router.post(
    "/configs/{config_id}/start",
    dependencies=[Depends(require_plan(CAP_MARKET_MAKER))],
)
async def start_market_maker_endpoint(
    config_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Start the market maker loop for a config."""
    user_id = current_user.get("user_id")
    row = get_market_maker_config_for_user(db, config_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    set_market_maker_config_state(db, row, enabled=True)

    started = await start_market_maker(config_id)
    return {"status": "started" if started else "already_running", "config_id": config_id}


@router.post("/configs/{config_id}/stop")
async def stop_market_maker_endpoint(
    config_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Stop the market maker loop for a config."""
    user_id = current_user.get("user_id")
    row = get_market_maker_config_for_user(db, config_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    set_market_maker_config_state(db, row, enabled=False)

    stopped = await stop_market_maker(config_id)
    return {"status": "stopped" if stopped else "not_running", "config_id": config_id}


@router.post(
    "/configs/{config_id}/sync",
    response_model=MarketMakerSyncResponse,
    dependencies=[Depends(require_plan(CAP_MARKET_MAKER))],
)
async def sync_market_maker_endpoint(
    config_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Manually trigger one sync cycle."""
    user_id = current_user.get("user_id")
    row = get_market_maker_config_for_user(db, config_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")

    result = await trigger_single_sync(config_id)
    if result.get("error"):
        return MarketMakerSyncResponse(success=False, detail=result["error"])
    return MarketMakerSyncResponse(success=True, result=result)


@router.get("/metrics", response_model=MarketMakerMetricsResponse)
async def get_market_maker_metrics(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get running market maker metrics."""
    running = get_running_maker_ids()
    return MarketMakerMetricsResponse(
        running_configs=running,
        total_running=len(running),
    )


@router.get("/configs/{config_id}/paper-intents")
def get_paper_intents(
    config_id: int, db: Session = Depends(get_db), account=Depends(get_active_account)
):
    """Read quote definitions in the active account; definitions never prove fills."""
    import json

    from app.models.trading.paper_maker_intent import PaperMakerIntent

    config = get_market_maker_config_for_user(db, config_id, account.owner_user_id)
    if config is None or not (
        config.account_id == account.id or (config.account_id is None and account.is_default)
    ):
        raise HTTPException(404, "Maker config not found")
    rows = (
        db.query(PaperMakerIntent)
        .filter_by(config_id=config_id, user_id=account.owner_user_id)
        .order_by(PaperMakerIntent.updated_at.desc(), PaperMakerIntent.id)
        .limit(100)
        .all()
    )
    return {
        "policy": "maker-no-evidence-no-fill-v1",
        "fill_status": "unfilled_missing_flow_and_queue_evidence",
        "config_enabled": bool(config.enabled),
        "intents": [
            {
                "id": row.id,
                "state": row.state,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                **json.loads(row.payload_json),
            }
            for row in rows
        ],
    }
