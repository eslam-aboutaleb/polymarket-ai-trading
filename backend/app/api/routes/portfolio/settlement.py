"""Read-only advisory settlement lifecycle & redemption risk API; never executes or signs."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.users.account import Account
from app.services.analytics.settlement_lifecycle import (
    SettlementAnalysisRequest,
    SettlementLifecycleReport,
    analyze_settlement_lifecycle,
)
from app.services.analytics.settlement_read_side import RecordedSettlementReport
from app.services.trading.position_lifecycle_service import read_recorded_settlement
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settlement",
    tags=["settlement lifecycle"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().settlement_redemption_enabled:
        raise HTTPException(503, "Settlement lifecycle analysis disabled")


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return {
        "input_schema": SettlementAnalysisRequest.model_json_schema(),
        "output_schema": SettlementLifecycleReport.model_json_schema(),
    }


@router.post("/analyze", dependencies=[Depends(enabled)])
def analyze(payload: SettlementAnalysisRequest) -> SettlementLifecycleReport:
    report = analyze_settlement_lifecycle(payload)
    for discrepancy in report.discrepancies:
        logger.warning("settlement_scenario_discrepancy code=%s", discrepancy.code)
    return report


@router.get("/recorded", dependencies=[Depends(enabled)])
async def recorded(
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> RecordedSettlementReport:
    return await read_recorded_settlement(db, account)
