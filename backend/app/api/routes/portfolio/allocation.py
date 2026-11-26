"""Advisory scenario allocation. Cannot place, size-authorize or cancel orders."""

import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.analytics.portfolio_scenario import PortfolioScenario
from app.models.users.account import Account
from app.services.portfolio.capital_allocator import (
    AllocationProposal,
    AllocationRequest,
    allocate,
)
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/v1/portfolio",
    tags=["portfolio allocation"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled() -> None:
    if not get_settings().portfolio_allocation_enabled:
        raise HTTPException(503, "Portfolio allocation simulation disabled")


@router.post("/allocation/simulate", dependencies=[Depends(enabled)])
def simulate(
    payload: AllocationRequest,
    persist: bool = False,
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> dict:
    try:
        proposal = allocate(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    body = proposal.model_dump_json()
    scenario_id = None
    if persist:
        raw = payload.model_dump_json()
        scenario_id = str(uuid.uuid4())
        db.add(
            PortfolioScenario(
                id=scenario_id,
                user_id=account.owner_user_id,
                account_id=account.id,
                input_json=raw,
                input_hash=hashlib.sha256(raw.encode()).hexdigest(),
                proposal_json=body,
                allocator_version=proposal.allocator_version,
            )
        )
        db.commit()
    return {"scenario_id": scenario_id, "proposal": json.loads(body)}


@router.get("/scenarios", dependencies=[Depends(enabled)])
def scenarios(
    limit: int = 20,
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
) -> list[dict]:
    rows = (
        db.query(PortfolioScenario)
        .filter(
            PortfolioScenario.user_id == account.owner_user_id,
            PortfolioScenario.account_id == account.id,
        )
        .order_by(PortfolioScenario.created_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return [
        {
            "scenario_id": r.id,
            "created_at": r.created_at.isoformat(),
            "input_hash": r.input_hash,
            "allocator_version": r.allocator_version,
            "proposal": AllocationProposal.model_validate_json(r.proposal_json).model_dump(
                mode="json"
            ),
        }
        for r in rows
    ]
