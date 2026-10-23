"""Authenticated, account-scoped research replay; no wallet/execution dependencies."""

import hashlib
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_active_account
from app.config import get_settings
from app.models.analytics.follower_experiment import FollowerExperimentRun
from app.models.users.account import Account
from app.services.analytics.replay.follower import FollowerExperiment, replay
from app.utils.database import get_db

router = APIRouter(prefix="/api/copy-trading/experiments", tags=["follower research"])
logger = logging.getLogger(__name__)


def enabled():
    if not get_settings().follower_replay_enabled:
        raise HTTPException(503, "Follower replay is not enabled on this deployment")


def response(row: FollowerExperimentRun, *, detail: bool = True):
    data = {"id": row.id, "input_hash": row.input_hash, "created_at": row.created_at.isoformat()}
    if detail:
        data.update(input=json.loads(row.input_json), result=json.loads(row.result_json))
    return data


def _save_experiment(
    body: FollowerExperiment,
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
    capture_ids: list[str] | None = None,
):
    payload = body.model_dump_json()
    try:
        result = replay(body)
    except ValueError as exc:
        raise HTTPException(422, "Inconsistent replay events") from exc
    if capture_ids is not None:
        from app.services.market_data.replay_capture import capture_manifest, digest

        manifest = capture_manifest(db, capture_ids)
        result.update(
            capture_manifest=manifest,
            capture_manifest_hash=digest(manifest),
            quality="native_captured_modeled",
            capture_ids=capture_ids,
            historical_backfill=False,
        )
    row = FollowerExperimentRun(
        id=str(uuid4()),
        user_id=account.owner_user_id,
        account_id=account.id,
        input_hash=hashlib.sha256(payload.encode()).hexdigest(),
        input_json=payload,
        result_json=json.dumps(result, sort_keys=True),
    )
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Follower experiment persistence failed account=%s", account.id)
        raise HTTPException(503, "Could not save follower experiment") from exc
    logger.info(
        "Follower experiment saved account=%s run=%s hash=%s", account.id, row.id, row.input_hash
    )
    return response(row)


@router.post("", dependencies=[Depends(enabled)])
def create_experiment(
    body: FollowerExperiment,
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    return _save_experiment(body, account, db)


@router.post("/from-capture", dependencies=[Depends(enabled)])
def create_captured_experiment(
    body: FollowerExperiment,
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    from app.services.market_data.replay_capture import frozen_dataset
    from app.utils.time import utc_now

    if body.end_at > utc_now():
        raise HTTPException(422, "Captured replay end must not be in the future")
    try:
        dataset, refs = frozen_dataset(db, body.wallet.lower(), body.start_at, body.end_at)
        validated = FollowerExperiment.model_validate({**body.model_dump(), "dataset": dataset})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not dataset.signals:
        raise HTTPException(
            409,
            "No signals captured in this receipt-time window; "
            "earlier history cannot be reconstructed",
        )
    return _save_experiment(validated, account, db, capture_ids=refs)


def scoped(db: Session, account: Account):
    return db.query(FollowerExperimentRun).filter_by(
        user_id=account.owner_user_id, account_id=account.id
    )


@router.get("", dependencies=[Depends(enabled)])
def list_experiments(
    limit: int = Query(20, ge=1, le=100),
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    return {
        "runs": [
            response(row, detail=False)
            for row in scoped(db, account)
            .order_by(FollowerExperimentRun.created_at.desc(), FollowerExperimentRun.id.desc())
            .limit(limit)
        ]
    }


@router.get("/{run_id}", dependencies=[Depends(enabled)])
def get_experiment(
    run_id: str, account: Account = Depends(get_active_account), db: Session = Depends(get_db)
):
    row = scoped(db, account).filter_by(id=run_id).first()
    if row is None:
        raise HTTPException(404, "Follower experiment not found")
    return response(row)
