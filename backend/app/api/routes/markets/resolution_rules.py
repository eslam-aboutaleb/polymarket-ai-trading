"""Read-only analytical resolution rule tooling; no signing or order execution."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.intelligence.resolution_rules import (
    ContractSpecification,
    ResolutionRuleExplanation,
    explain_resolution_rule,
    invalidate_dependent_forecasts,
    record_rule_snapshot,
)
from app.services.market_data.polymarket_service import PolymarketService
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/resolution-rules",
    tags=["resolution rule analysis"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().resolution_rule_analysis_enabled:
        raise HTTPException(503, "Resolution rule analysis disabled")


class RuleAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    raw_market: dict[str, Any]
    as_of: datetime | None = None
    clarifications: list[str] = Field(default_factory=list, max_length=20)
    persist_snapshot: bool = False


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return {
        "specification_schema": ContractSpecification.model_json_schema(),
        "explanation_schema": ResolutionRuleExplanation.model_json_schema(),
    }


@router.get("/explain", dependencies=[Depends(enabled)])
async def explain(
    condition_id: str = Query(pattern=r"^0x[a-fA-F0-9]{64}$"),
    as_of: datetime | None = None,
    db: Session = Depends(get_db),
) -> ResolutionRuleExplanation:
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(422, "as_of requires a timezone")
    try:
        return await explain_resolution_rule(
            condition_id,
            as_of=as_of,
            db=db,
            service=PolymarketService(),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/analyze", dependencies=[Depends(enabled)])
async def analyze(
    payload: RuleAnalyzeRequest,
    db: Session = Depends(get_db),
) -> ResolutionRuleExplanation:
    if payload.as_of is not None and payload.as_of.tzinfo is None:
        raise HTTPException(422, "as_of requires a timezone")
    if payload.persist_snapshot:
        raise HTTPException(422, "Caller-supplied rules cannot establish an official snapshot")
    try:
        result = await explain_resolution_rule(
            payload.condition_id,
            as_of=payload.as_of,
            db=db,
            raw_market=payload.raw_market,
            clarifications=payload.clarifications,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result


@router.post("/refresh", dependencies=[Depends(enabled)])
async def refresh(
    condition_id: str = Query(pattern=r"^0x[a-fA-F0-9]{64}$"),
    db: Session = Depends(get_db),
) -> ResolutionRuleExplanation:
    """Capture official public metadata at server receipt time, without backdating."""
    result = await explain_resolution_rule(condition_id, db=db, service=PolymarketService())
    record_rule_snapshot(db, result.specification)
    if result.change_report and result.change_report.invalidates_forecasts:
        invalidate_dependent_forecasts(db, condition_id, result.specification.source_hash)
    db.commit()
    return result
