"""Owned durable redemption previews and receipt reconciliation; no submission endpoint."""

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.models import Account
from app.models.trading.redemption_intent import RedemptionIntent, RedemptionObservation
from app.security.rate_limit import limiter
from app.services.analytics.live_account_reconciliation import digest
from app.services.trading import redemption_workflow as workflow
from app.services.trading.execution.async_io import run_blocking_execution
from app.utils.database import get_db


def private(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, X-Account-Id"


router = APIRouter(
    prefix="/api/portfolio/redemption-intents",
    tags=["redemption evidence"],
    dependencies=[Depends(private)],
)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")
    execution_mode: Literal["preview"] = "preview"


class IntentResponse(BaseModel):
    id: int
    intent_id: str
    user_id: int
    account_id: int
    market_id: str
    collection_id: str
    execution_mode: str
    status: str
    preview_only: bool
    on_chain_submitted: bool
    broadcast_outcome_unknown: bool
    tx_hash: str | None
    amount: float
    expected_payout: str
    finalized_payout: str | None
    nonce: int | None
    snapshot_hash: str
    receipt_hash: str | None
    error: str | None
    created_at: datetime
    finality_policy: str
    execution_eligible: Literal[False]


def run(db, account, operation):
    try:
        return workflow.response(db, operation())
    except LookupError as exc:
        raise HTTPException(404, "Redemption intent unavailable") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(503, "Redemption evidence unavailable; nothing was submitted") from exc


@router.post("", response_model=IntentResponse)
@limiter.limit("4/minute")
async def preview(
    request: Request,
    body: PreviewRequest,
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
):
    return await run_blocking_execution(
        run,
        db,
        account,
        lambda: workflow.create_intent(db, account, body.condition_id, body.idempotency_key),
    )


@router.get("", response_model=list[IntentResponse])
def history(
    limit: int = Query(50, ge=1, le=100),
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(RedemptionIntent)
        .where(
            RedemptionIntent.user_id == account.owner_user_id,
            RedemptionIntent.account_id == account.id,
        )
        .order_by(RedemptionIntent.created_at.desc(), RedemptionIntent.id)
        .limit(limit)
    ).all()
    return [
        run(db, account, lambda row=row: workflow.read_intent(db, account, row.id)) for row in rows
    ]


@router.get("/{intent_id}", response_model=IntentResponse)
def detail(
    intent_id: str = Path(pattern=r"^[a-f0-9]{64}$"),
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
):
    return run(db, account, lambda: workflow.read_intent(db, account, intent_id))


@router.post("/{intent_id}/reconcile", response_model=IntentResponse)
@limiter.limit("4/minute")
async def reconcile(
    request: Request,
    intent_id: str = Path(pattern=r"^[a-f0-9]{64}$"),
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
):
    return await run_blocking_execution(
        run, db, account, lambda: workflow.reconcile_intent(db, account, intent_id)
    )


@router.get("/{intent_id}/evidence")
def evidence(
    intent_id: str = Path(pattern=r"^[a-f0-9]{64}$"),
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
):
    try:
        row = workflow.read_intent(db, account, intent_id)
    except LookupError as exc:
        raise HTTPException(404, "Redemption intent unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "Redemption evidence integrity mismatch") from exc
    observations = db.scalars(
        select(RedemptionObservation)
        .where(
            RedemptionObservation.intent_id == row.id,
        )
        .order_by(RedemptionObservation.observed_at.desc())
        .limit(50)
    ).all()
    result = []
    for observation in observations:
        proof = json.loads(observation.proof_json)
        if digest(proof) != observation.proof_hash:
            raise HTTPException(409, "Redemption observation integrity mismatch")
        result.append(
            {
                "proof_hash": observation.proof_hash,
                "observed_at": observation.observed_at,
                "proof": proof,
            }
        )
    return {
        "intent_id": row.id,
        "snapshot_hash": row.snapshot_hash,
        "snapshot": json.loads(row.snapshot_json),
        "observations": result,
        "observation_limit": 50,
    }
