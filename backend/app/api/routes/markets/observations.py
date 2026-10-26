"""Authenticated read-only public observation archive. No client-supplied receipts."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.services.market_data.replay_capture import aware
from app.services.market_data.venue_observations import history, serialize
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(
    prefix="/api/market-data",
    tags=["market data"],
    dependencies=[Depends(get_current_user_from_token)],
)


@router.get("/observations")
def observations(
    token_id: str = Query(pattern=r"^[0-9]{1,120}$"),
    as_of: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    if not get_settings().observation_archive_enabled:
        raise HTTPException(503, "Observation archive disabled")
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(422, "as_of requires a timezone")
    cutoff = as_of or utc_now()
    rows = history(db, token_id, cutoff, limit)
    return {
        "as_of": cutoff.isoformat(),
        "quality": "INSUFFICIENT_DATA",
        "reason": "Raw observations do not prove continuous reconstructed depth",
        "latest_receipt_at": rows[0].received_at.isoformat() if rows else None,
        "receipt_age_seconds": max(0, (cutoff - aware(rows[0].received_at)).total_seconds())
        if rows
        else None,
        "observations": [serialize(row) for row in rows],
    }
