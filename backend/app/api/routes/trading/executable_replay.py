"""Account-scoped frozen research. No order execution capability."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.markets.microstructure import owned_account
from app.config import get_settings
from app.models.analytics.executable_replay import ExecutableReplayRun
from app.security.rate_limit import limiter
from app.services.analytics.replay import executable_archive as archive
from app.services.analytics.replay.executable import replay
from app.services.analytics.replay.executable_contracts import ReplayDefinition
from app.services.market_data.binary_book import Contract
from app.utils.database import get_db


def enabled():
    if not get_settings().executable_replay_enabled:
        raise HTTPException(503, "Executable replay disabled")


router = APIRouter(
    prefix="/api/backtesting/executable",
    tags=["executable research"],
    dependencies=[Depends(enabled)],
)


class FreezeRequest(Contract):
    definition: ReplayDefinition
    capture_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] | None = Field(
        None, min_length=1, max_length=250
    )
    follower_wallet: str | None = Field(None, pattern=r"^0x[a-fA-F0-9]{40}$")


def present(row):
    return {
        "id": row.id,
        "input_hash": row.input_hash,
        "evidence_hash": row.evidence_hash,
        "result_hash": row.result_hash,
        "definition": json.loads(row.input_json),
        "evidence": json.loads(row.evidence_json),
        "result": json.loads(row.result_json) if row.result_json else None,
        "frozen_at": row.frozen_at.isoformat(),
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "research_only": True,
    }


def owned(db, account, identity):
    row = (
        db.query(ExecutableReplayRun)
        .filter_by(id=str(identity), account_id=account.id, user_id=account.owner_user_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Replay not found")
    return row


@router.get("/schema")
def schema(account=Depends(owned_account)):
    return {"research_only": True, "schema": FreezeRequest.model_json_schema()}


@router.get("/runs")
def listing(
    limit: int = Query(20, ge=1, le=100),
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(ExecutableReplayRun)
        .filter_by(account_id=account.id, user_id=account.owner_user_id)
        .order_by(ExecutableReplayRun.frozen_at.desc())
        .limit(limit)
        .all()
    )
    return {"runs": [present(row) for row in rows]}


@router.post("/runs")
@limiter.limit("2/minute")
def freeze(
    request: Request,
    body: FreezeRequest,
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    try:
        row = archive.freeze(db, account, body.definition, body.capture_ids, body.follower_wallet)
        db.commit()
        return present(row)
    except (ValueError, KeyError, ArithmeticError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(409, "Replay evidence could not be frozen") from exc


@router.get("/runs/{run_id}")
def get_run(run_id: UUID, account=Depends(owned_account), db: Session = Depends(get_db)):
    row = owned(db, account, run_id)
    try:
        archive.verify(row, db)
        return present(row)
    except (ValueError, KeyError, ArithmeticError) as exc:
        raise HTTPException(409, "Replay integrity check failed") from exc


@router.post("/runs/{run_id}/execute")
@limiter.limit("2/minute")
def execute(
    request: Request,
    run_id: UUID,
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    row = owned(db, account, run_id)
    try:
        row = archive.execute(db, account, row)
        db.commit()
        return present(row)
    except (ValueError, KeyError, ArithmeticError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(409, "Frozen replay could not be evaluated") from exc


@router.post("/runs/{run_id}/verify")
@limiter.limit("2/minute")
def verify(
    request: Request,
    run_id: UUID,
    account=Depends(owned_account),
    db: Session = Depends(get_db),
):
    row = owned(db, account, run_id)
    try:
        definition, evidence = archive.verify(row, db)
        if row.result_json is None:
            raise ValueError("run not evaluated")
        result = replay(definition)
        result.update(
            source_evidence=evidence,
            execution_evidence=(
                "native_captured_counterfactual"
                if row.dataset_id
                else "imported_unverified_modeled"
            ),
        )
        if result != json.loads(row.result_json):
            raise ValueError("replay mismatch")
        return {"identical": True, "input_hash": row.input_hash, "result_hash": row.result_hash}
    except (ValueError, KeyError, ArithmeticError) as exc:
        raise HTTPException(409, "Replay integrity check failed") from exc
