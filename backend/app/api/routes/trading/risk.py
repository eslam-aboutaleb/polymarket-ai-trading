"""Stage-0 risk controls. Financial authority stays unavailable until reconciled."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.trading.trading_control import TradingControlState
from app.models.users.user import User
from app.services.trading.trading_halt_service import read_control, request_halt
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])


class HaltRequest(BaseModel):
    scope: Literal["user", "global"] = "user"


@router.get("/summary")
def risk_summary(
    current_user: dict = Depends(get_current_user_from_token), db: Session = Depends(get_db)
):
    return {
        "global": read_control(db, "global"),
        "wallet": read_control(db, f"user:{current_user['user_id']}"),
        "live_trading_enabled": get_settings().live_trading_enabled,
        "live_execution_available": False,
        "financial_state": "UNVERIFIED",
        "release_blocker": "DURABLE_EXECUTION_GATEWAY_REQUIRED",
    }


@router.post("/halt", status_code=202)
def halt(
    body: HaltRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    user_id = current_user["user_id"]
    if body.scope == "global":
        actor = db.get(User, user_id, populate_existing=True)
        if actor is None or not actor.is_admin:
            raise HTTPException(status_code=403, detail="Global emergency role required")
    operations = request_halt(db, user_id=user_id, global_halt=body.scope == "global")
    # Global operators do not gain access to other users' operation reports.
    return {
        "halt_status": "HALTING",
        "scope": body.scope,
        "operations_queued": len(operations),
        "operation_ids": [op.id for op in operations if op.user_id == user_id],
    }


class RecoveryRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@router.post("/recovery-request", status_code=202)
def recovery_request(
    body: RecoveryRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    control = (
        db.query(TradingControlState)
        .filter(TradingControlState.scope == f"user:{current_user['user_id']}")
        .with_for_update()
        .one_or_none()
    )
    if control is None:
        raise HTTPException(status_code=409, detail="No verified halt state")
    control.state = "RECOVERY_REQUIRED"
    control.reason = f"Recovery requested: {body.reason}"
    control.version += 1
    control.updated_at = utc_now()
    db.commit()
    return {"state": control.state, "resumed": False, "checks": "NOT_YET_IMPLEMENTED"}
