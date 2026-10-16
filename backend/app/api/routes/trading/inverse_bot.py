"""Inverse Position Bot API routes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_plan
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.trading.inverse_bot_position import InverseBotPosition
from app.services.trading.inverse_bot_monitor import (
    disable_inverse_bot_position as disable_inverse_bot_position_service,
)
from app.services.trading.inverse_bot_monitor import (
    get_inverse_bot_metrics,
    get_inverse_bot_position_for_user,
    manual_evaluate_inverse_position,
)
from app.services.trading.inverse_bot_monitor import (
    list_inverse_bot_positions as list_inverse_bot_positions_service,
)
from app.services.trading.inverse_bot_monitor import (
    upsert_inverse_bot_position as upsert_inverse_bot_position_service,
)
from app.services.users.entitlement_service import CAP_INVERSE_BOT
from app.utils.database import get_db

router = APIRouter(prefix="/api/inverse-bot", tags=["inverse-bot"])


class InverseBotPositionRequest(BaseModel):
    token_id: str = Field(..., min_length=2)
    condition_id: str = Field(..., min_length=2)
    market_title: str = ""
    outcome: str = ""
    enabled: bool = True
    size_mode_override: str = Field(
        default="inherit",
        pattern="^(inherit|full_notional|fixed_amount)$",
    )
    fixed_amount_override: float | None = Field(default=None, gt=0)


class InverseBotPositionResponse(BaseModel):
    id: int
    token_id: str
    condition_id: str
    market_title: str
    outcome: str
    enabled: bool
    size_mode_override: str
    fixed_amount_override: float | None
    status: str
    last_signal: str | None
    last_confidence: float | None
    last_reasoning: str | None
    last_web_summary: str | None
    last_x_summary: str | None
    last_error: str | None
    last_recommendation: str | None
    last_alt_outcome: str | None
    last_alt_token_id: str | None
    last_evaluated_at: str | None
    last_reversed_at: str | None
    reversals_today: int
    reversals_day: str | None
    persistence_count: int
    created_at: str
    updated_at: str | None


class ManualEvaluateResponse(BaseModel):
    success: bool
    result: dict[str, Any] | None = None
    detail: str | None = None


def _to_response(row: InverseBotPosition) -> InverseBotPositionResponse:
    return InverseBotPositionResponse(
        id=row.id,
        token_id=row.token_id,
        condition_id=row.condition_id,
        market_title=row.market_title or "",
        outcome=row.outcome or "",
        enabled=bool(row.enabled),
        size_mode_override=row.size_mode_override or "inherit",
        fixed_amount_override=row.fixed_amount_override,
        status=row.status or "active",
        last_signal=row.last_signal,
        last_confidence=row.last_confidence,
        last_reasoning=row.last_reasoning,
        last_web_summary=row.last_web_summary,
        last_x_summary=row.last_x_summary,
        last_error=row.last_error,
        last_recommendation=row.last_recommendation,
        last_alt_outcome=row.last_alt_outcome,
        last_alt_token_id=row.last_alt_token_id,
        last_evaluated_at=row.last_evaluated_at.isoformat() if row.last_evaluated_at else None,
        last_reversed_at=row.last_reversed_at.isoformat() if row.last_reversed_at else None,
        reversals_today=int(row.reversals_today or 0),
        reversals_day=row.reversals_day.isoformat() if row.reversals_day else None,
        persistence_count=int(row.persistence_count or 0),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.get("/positions", response_model=list[InverseBotPositionResponse])
async def list_inverse_bot_positions(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    user_id = current_user.get("user_id")
    rows = list_inverse_bot_positions_service(db, user_id)
    return [_to_response(r) for r in rows]


@router.post(
    "/positions",
    response_model=InverseBotPositionResponse,
    dependencies=[Depends(require_plan(CAP_INVERSE_BOT))],
)
async def upsert_inverse_bot_position(
    body: InverseBotPositionRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    user_id = current_user.get("user_id")
    row = upsert_inverse_bot_position_service(db, user_id, body.model_dump())
    return _to_response(row)


@router.delete("/positions/{position_id}")
async def disable_inverse_bot_position(
    position_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    user_id = current_user.get("user_id")
    row = get_inverse_bot_position_for_user(db, position_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Inverse bot position not found")
    disable_inverse_bot_position_service(db, row)
    return {"status": "disabled", "id": position_id}


@router.post(
    "/positions/{position_id}/evaluate",
    response_model=ManualEvaluateResponse,
    dependencies=[Depends(require_plan(CAP_INVERSE_BOT))],
)
async def evaluate_inverse_bot_position(
    position_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    user_id = current_user.get("user_id")
    result = await manual_evaluate_inverse_position(db, user_id, position_id)
    if not result.get("success"):
        return ManualEvaluateResponse(
            success=False,
            detail=result.get("detail", "Evaluation failed"),
        )
    return ManualEvaluateResponse(success=True, result=result.get("result"))


@router.get("/metrics")
async def get_inverse_bot_monitor_metrics(
    current_user: dict = Depends(get_current_user_from_token),
):
    # Intentionally available to authenticated users for diagnostics.
    return get_inverse_bot_metrics()
