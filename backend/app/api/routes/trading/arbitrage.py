"""Read-only arbitrage candidates and paper-only feasibility (issue #9).

Nothing here submits, signs or cancels an order. Candidates from the scanner are
INDICATIVE; feasibility is computed from frozen native book receipts at an explicit
``as_of`` and is persisted only as account-owned research evidence.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.microstructure import owned_account
from app.config import get_settings
from app.models import Account
from app.models.analytics.arbitrage_feasibility_run import ArbitrageFeasibilityRun
from app.services.analytics import arbitrage_service
from app.services.analytics.arbitrage import ENGINE_VERSION
from app.services.analytics.arbitrage.feasibility_service import (
    FeasibilityRequest,
    assess_frozen,
    replay_run,
    save_run,
)
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/v1/arbitrage",
    tags=["arbitrage feasibility"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled():
    if not get_settings().arbitrage_feasibility_enabled:
        raise HTTPException(503, "Arbitrage feasibility disabled")


@router.get("/candidates")
def list_candidates():
    """INDICATIVE candidates from the latest scans; none is executable."""
    candidates = arbitrage_service.get_recent_candidates()
    return {
        "schema_version": arbitrage_service.SCAN_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "count": len(candidates),
        "candidates": candidates,
        "paper_only": True,
        "live_execution_enabled": False,
    }


@router.post("/{candidate_id}/feasibility", dependencies=[Depends(enabled)])
def feasibility(
    candidate_id: str,
    request: FeasibilityRequest,
    account: Account = Depends(owned_account),
    db: Session = Depends(get_db),
):
    candidate = arbitrage_service.get_candidate(candidate_id)
    if candidate is None or not candidate["condition_ids"]:
        raise HTTPException(404, "Arbitrage candidate not found")
    if candidate["kind"] != "paired_outcome":
        raise HTTPException(409, "Cross-venue candidates stay review-only")
    condition_id = candidate["condition_ids"][0].lower()
    try:
        if request.persist:
            saved = save_run(db, condition_id, request, account)
            db.commit()
            return saved
        return assess_frozen(db, condition_id, request)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(409, "No captured book evidence at the requested as_of") from exc
    except (ValueError, ArithmeticError) as exc:
        db.rollback()
        raise HTTPException(409, "Captured book evidence failed validation") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(409, "Feasibility run could not be frozen") from exc


@router.get("/runs/{run_id}", dependencies=[Depends(enabled)])
def replay(run_id: str, account: Account = Depends(owned_account), db: Session = Depends(get_db)):
    row = (
        db.query(ArbitrageFeasibilityRun)
        .filter_by(id=run_id, user_id=account.owner_user_id, account_id=account.id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Feasibility run not found")
    try:
        return replay_run(db, row)
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, "Feasibility run integrity check failed") from exc
