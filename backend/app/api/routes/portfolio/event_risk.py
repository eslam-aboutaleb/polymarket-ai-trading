"""Read-only advisory event-correlation risk tooling; never overrides pre_trade_gate."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.users.account import Account
from app.services.analytics.event_correlation_risk import (
    EventCorrelationRequest,
    EventRiskAssessment,
    assess_event_correlation_risk,
)
from app.services.analytics.portfolio_risk_service import recorded_event_risk
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/risk/event-correlation",
    tags=["event correlation risk"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().event_correlation_risk_enabled:
        raise HTTPException(503, "Event correlation risk analysis disabled")


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return {
        "input_schema": EventCorrelationRequest.model_json_schema(),
        "output_schema": EventRiskAssessment.model_json_schema(),
    }


@router.post("/analyze", dependencies=[Depends(enabled)])
def analyze(payload: EventCorrelationRequest) -> EventRiskAssessment:
    return assess_event_correlation_risk(payload)


@router.get("/recorded", dependencies=[Depends(enabled)])
def recorded(
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> EventRiskAssessment:
    return recorded_event_risk(db, account)
