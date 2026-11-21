"""Bounded account-owned reconciliation, separate from execution authority."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.config import get_settings
from app.models.users.account import Account
from app.security.rate_limit import limiter
from app.services.analytics.live_account_reconciliation import (
    build_reconciliation_report,
    capture_balance_snapshot,
    snapshot_list,
)
from app.services.trading.execution.async_io import run_blocking_execution
from app.utils.database import get_db


def enabled(response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, X-Account-Id"
    if not get_settings().live_account_reconciliation_enabled:
        raise HTTPException(503, "Live reconciliation unavailable")


router = APIRouter(
    prefix="/api/trades/analytics", tags=["live reconciliation"], dependencies=[Depends(enabled)]
)


@router.get("/balance-snapshots")
def snapshots(account: Account = Depends(get_strict_account), db: Session = Depends(get_db)):
    return snapshot_list(db, account)


@router.post("/balance-snapshots")
@limiter.limit("2/minute")
async def capture(
    request: Request, account: Account = Depends(get_strict_account), db: Session = Depends(get_db)
):
    try:
        return await run_blocking_execution(capture_balance_snapshot, db, account)
    except ValueError as exc:
        reason = str(exc)
        if reason in {
            "ACCOUNT_OWNER_UNAVAILABLE",
            "SUBACCOUNT_WALLET_ALLOCATION_UNVERIFIED",
            "ORDER_HISTORY_BOUND_EXCEEDED",
            "SHARED_WALLET_ACCOUNT_ALLOCATION_UNVERIFIED",
            "KNOWN_TOKEN_OBSERVATION_BOUND_EXCEEDED",
            "AUTHENTICATED_READ_CREDENTIALS_REQUIRED",
            "BALANCE_OBSERVATION_DEADLINE_EXCEEDED",
            "BALANCE_CHANGED_DURING_OBSERVATION",
            "ACTIVITY_CHANGED_DURING_OBSERVATION",
        }:
            raise HTTPException(422, reason) from exc
        raise HTTPException(503, "Native balance observation unavailable") from exc
    except Exception as exc:
        raise HTTPException(503, "Native balance observation unavailable") from exc


@router.get("/reconciliation")
def report(
    opening_id: str = Query(pattern=r"^[a-f0-9]{64}$"),
    closing_id: str = Query(pattern=r"^[a-f0-9]{64}$"),
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
):
    try:
        return build_reconciliation_report(db, account, opening_id, closing_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(422, str(exc)) from exc
