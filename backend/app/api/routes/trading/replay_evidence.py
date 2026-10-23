"""Bounded public venue capture. Saved replay runs retain account ownership."""

import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_active_account
from app.config import get_settings
from app.models.market_data.replay_capture import ReplayCapture
from app.models.users.account import Account
from app.security.rate_limit import limiter
from app.services.market_data.replay_capture import (
    PublicVenueCapture,
    capture_wallet,
    get_capture_venue,
)
from app.utils.database import get_db

router = APIRouter(prefix="/api/replay", tags=["native replay evidence"])


class WalletRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wallet: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")


@router.post("/capture")
@limiter.limit("6/minute")
async def capture(
    request: Request,
    body: WalletRequest,
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
    venue: PublicVenueCapture = Depends(get_capture_venue),
):
    if not get_settings().native_capture_enabled:
        raise HTTPException(503, "Native capture disabled")
    try:
        async with asyncio.timeout(90):
            return await capture_wallet(db, body.wallet.lower(), venue)
    except (ValueError, httpx.HTTPError, TimeoutError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(
            503, "Native capture unavailable; no historical data was invented"
        ) from exc


@router.get("/captures/{capture_id}")
def get_capture(
    capture_id: str,
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    # Captures contain only public venue evidence; no account/provider credentials.
    row = db.get(ReplayCapture, capture_id)
    if row is None:
        raise HTTPException(404, "Captured public evidence not found")
    return {
        "id": row.id,
        "kind": row.kind,
        "condition_id": row.condition_id,
        "token_id": row.token_id,
        "wallet": row.wallet,
        "observed_at": row.observed_at.isoformat(),
        "source_at": row.source_at.isoformat() if row.source_at else None,
        "content_hash": row.content_hash,
        "rule_hash": row.rule_hash,
        "payload": json.loads(row.payload_json),
    }
