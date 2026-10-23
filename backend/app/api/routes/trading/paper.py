"""Explicit paper-only account operations. No wallet credentials or order signer."""

import asyncio
import json
from decimal import Decimal
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.config import get_settings
from app.models import Account, PaperAccount, PaperJournalEvent
from app.security.rate_limit import limiter
from app.services.analytics import paper_accounting as paper
from app.services.market_data.replay_capture import (
    PublicVenueCapture,
    aware,
    capture_market,
    get_capture_venue,
)
from app.utils.database import get_db

router = APIRouter(prefix="/api/paper", tags=["paper account"])


class Configure(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    fee_bps: Decimal = Field(ge=0, le=10000, decimal_places=8)
    funding: Decimal | None = Field(default=None, ge=0, le=1_000_000_000, decimal_places=8)
    acknowledge_legacy: bool = False
    policy: paper.PaperPolicy | None = None


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    token_id: str = Field(pattern=r"^[0-9]{1,120}$")
    side: Literal["BUY", "SELL"]
    amount: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=8)
    units: Literal["USDC", "SHARES"]
    intent_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")
    limit_price: Decimal | None = Field(default=None, gt=0, le=1, decimal_places=8)


class CashFlow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    amount: Decimal = Field(ge=-1_000_000_000, le=1_000_000_000, decimal_places=8)
    event_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")


class Market(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")


def enabled():
    if not get_settings().paper_journal_v2_enabled:
        raise HTTPException(503, "Paper journal disabled")


def commit(db, operation):
    try:
        result = operation()
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.get("/account")
def account(account: Account = Depends(get_strict_account), db: Session = Depends(get_db)):
    enabled()
    state = db.get(PaperAccount, account.id)
    if state is None:
        return {"initialized": False, "account_id": account.id}
    return {"initialized": True, **paper.value_account(db, state)}


@router.post("/account")
def configure(
    body: Configure, account: Account = Depends(get_strict_account), db: Session = Depends(get_db)
):
    enabled()

    def apply():
        state = paper.configure_account(
            db,
            account.owner_user_id,
            account.id,
            fee_bps=body.fee_bps,
            funding=body.funding,
            acknowledge_legacy=body.acknowledge_legacy,
            policy=body.policy.model_dump() if body.policy else None,
        )
        return {"initialized": True, **paper.value_account(db, state)}

    return commit(db, apply)


@router.post("/orders")
def submit(
    body: Order, account: Account = Depends(get_strict_account), db: Session = Depends(get_db)
):
    enabled()
    return commit(
        db,
        lambda: paper.submit_paper_order(
            db,
            user_id=account.owner_user_id,
            account_id=account.id,
            strategy="manual",
            **body.model_dump(),
        ),
    )


@router.post("/cash-flows")
def cash_flow(
    body: CashFlow, account: Account = Depends(get_strict_account), db: Session = Depends(get_db)
):
    enabled()
    return commit(
        db,
        lambda: paper.cash_flow(db, account.owner_user_id, account.id, body.event_id, body.amount),
    )


@router.post("/snapshot")
def snapshot(account: Account = Depends(get_strict_account), db: Session = Depends(get_db)):
    enabled()

    def apply():
        state = paper.account_state(db, account.owner_user_id, account.id)
        paper.settle_captured(db, account.owner_user_id, account.id)
        point = paper.capture_nav(db, state)
        return json.loads(point.valuation_json)

    return commit(db, apply)


@router.get("/journal")
def journal(
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
):
    enabled()
    rows = (
        db.query(PaperJournalEvent)
        .filter(
            PaperJournalEvent.account_id == account.id,
            PaperJournalEvent.user_id == account.owner_user_id,
            PaperJournalEvent.id > after,
        )
        .order_by(PaperJournalEvent.id)
        .limit(limit)
        .all()
    )
    return [
        {
            "sequence": row.id,
            "event_id": row.event_id,
            "observed_at": aware(row.observed_at).isoformat(),
            **json.loads(row.payload_json),
        }
        for row in rows
    ]


@router.post("/refresh-market")
@limiter.limit("6/minute")
async def refresh(
    request: Request,
    body: Market,
    account: Account = Depends(get_strict_account),
    db: Session = Depends(get_db),
    venue: PublicVenueCapture = Depends(get_capture_venue),
):
    enabled()
    if not get_settings().native_capture_enabled:
        raise HTTPException(503, "Native capture disabled")
    try:
        async with asyncio.timeout(30):
            captured = await capture_market(db, body.condition_id, venue)
        paper.settle_captured(db, account.owner_user_id, account.id)
        db.commit()
        return captured
    except (ValueError, httpx.HTTPError, TimeoutError) as exc:
        db.rollback()
        raise HTTPException(503, "Public evidence unavailable; no fills or marks invented") from exc
