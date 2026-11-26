"""Read-only execution cost estimate and shortfall quality. Never places an order."""

import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.microstructure import owned_account
from app.config import get_settings
from app.models import Account
from app.models.analytics.execution_estimate import ExecutionEstimateRecord
from app.security.rate_limit import limiter
from app.services.analytics.execution_quality import load_book, reconcile, record_estimate
from app.services.trading.execution.execution_estimator import (
    EstimateRequest,
    ExecutionEstimate,
    estimate_execution,
    revalidate_estimate,
)
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(
    prefix="/api/execution",
    tags=["execution estimates (read-only)"],
    dependencies=[Depends(get_current_user_from_token)],
)
logger = logging.getLogger(__name__)


def enabled():
    if not get_settings().execution_estimator_enabled:
        raise HTTPException(503, "Execution estimator disabled")


class EstimateBody(BaseModel):
    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    outcome: Literal["YES", "NO"] = "YES"
    max_age_seconds: int = Field(default=60, ge=1, le=300)
    request: EstimateRequest
    prior_estimate_record_id: str | None = Field(default=None, max_length=36)


def _owned_record(db, account, record_id):
    row = (
        db.query(ExecutionEstimateRecord)
        .filter_by(id=record_id, user_id=account.owner_user_id, account_id=account.id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Estimate not found")
    return row


def _revalidate(db, row, now):
    prior = ExecutionEstimate.model_validate(json.loads(row.estimate_json))
    outcome = "YES"
    spec, book, reason = load_book(db, row.condition_id, outcome, now)
    if spec is not None and row.token_id == spec.no_token:
        spec, book, reason = load_book(db, row.condition_id, "NO", now)
    result = revalidate_estimate(prior, book, now).model_dump(mode="json")
    if reason:
        result["load_reason"] = reason
    return result


@router.post("/estimate", dependencies=[Depends(enabled)])
@limiter.limit("60/minute")
def estimate(
    request: Request,
    body: EstimateBody,
    account: Account = Depends(owned_account),
    db: Session = Depends(get_db),
):
    now = utc_now()
    try:
        prior = (
            _revalidate(db, _owned_record(db, account, body.prior_estimate_record_id), now)
            if body.prior_estimate_record_id
            else None
        )
        spec, book, reason = load_book(
            db, body.condition_id.lower(), body.outcome, now, body.max_age_seconds
        )
        if spec is None:
            return {"status": "abstain", "reason": reason, "estimate": None, "prior": prior}
        result = estimate_execution(spec, book, body.request, now)
        row = record_estimate(db, account, result, body.condition_id.lower(), now)
        db.commit()
    except (ValueError, ArithmeticError, SQLAlchemyError) as exc:
        db.rollback()
        logger.warning("Execution estimate failed validation")
        raise HTTPException(409, "Captured book evidence failed validation") from exc
    return {
        "status": result.status,
        "reason": result.reason,
        "record_id": row.id,
        "estimate": result.model_dump(mode="json"),
        "prior": prior,
    }


@router.get("/estimates/{record_id}/revalidate", dependencies=[Depends(enabled)])
def revalidate(
    record_id: str, account: Account = Depends(owned_account), db: Session = Depends(get_db)
):
    return _revalidate(db, _owned_record(db, account, record_id), utc_now())


@router.get("/quality", dependencies=[Depends(enabled)])
def quality(
    window_days: int = Query(30, ge=1, le=365),
    account: Account = Depends(owned_account),
    db: Session = Depends(get_db),
):
    return reconcile(db, account, utc_now(), window_days)
