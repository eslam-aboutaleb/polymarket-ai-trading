"""Read-only analytical oracle basis risk tooling; no order execution."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.market_data.oracle_basis import (
    OracleBasisRequest,
    OracleBasisStudy,
)
from app.services.market_data.oracle_basis_archive import evaluate_archived_oracle_basis
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/oracle-basis",
    tags=["oracle basis risk"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().oracle_basis_risk_enabled:
        raise HTTPException(503, "Oracle basis risk analysis disabled")


@router.get("/schema", dependencies=[Depends(enabled)])
def schema() -> dict[str, Any]:
    return {
        "input_schema": OracleBasisRequest.model_json_schema(),
        "output_schema": OracleBasisStudy.model_json_schema(),
    }


@router.post("/analyze", dependencies=[Depends(enabled)])
def analyze(payload: OracleBasisRequest, db: Session = Depends(get_db)) -> OracleBasisStudy:
    return evaluate_archived_oracle_basis(db, payload)
