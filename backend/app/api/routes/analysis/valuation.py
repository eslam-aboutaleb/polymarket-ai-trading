"""Read-only analytical binary contract valuation; no order submission."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.intelligence.binary_valuation import (
    ValuationEvidence,
)
from app.services.intelligence.valuation_evidence import (
    RecordedValuationRequest,
    value_recorded_contract,
)
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/valuation",
    tags=["binary contract valuation"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().binary_valuation_enabled:
        raise HTTPException(503, "Binary contract valuation disabled")


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return {
        "input_schema": RecordedValuationRequest.model_json_schema(),
        "output_schema": ValuationEvidence.model_json_schema(),
    }


@router.post("/binary", dependencies=[Depends(enabled)])
def valuate_binary(
    payload: RecordedValuationRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user_from_token),
) -> ValuationEvidence:
    return value_recorded_contract(db, payload.arithmetic_input(), int(user["user_id"]))
