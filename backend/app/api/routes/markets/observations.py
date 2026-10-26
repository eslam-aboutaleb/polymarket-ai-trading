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
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
    db: Session = Depends(get_db),
):
    if not get_settings().observation_archive_enabled:
        raise HTTPException(503, "Observation archive disabled")
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(422, "as_of requires a timezone")
    cutoff = as_of or utc_now()
    try:
        rows = history(db, token_id, cutoff, limit, before=before)
        values = [serialize(row) for row in rows]
    except ValueError as exc:
        raise HTTPException(409, "Observation integrity or cursor check failed") from exc
    return {
        "as_of": cutoff.isoformat(),
        "quality": "INSUFFICIENT_DATA",
        "reason": "Raw observations do not prove continuous reconstructed depth",
        "latest_receipt_at": aware(rows[0].received_at).isoformat() if rows else None,
        "receipt_age_seconds": max(0, (cutoff - aware(rows[0].received_at)).total_seconds())
        if rows
        else None,
        "next_before": rows[-1].id if len(rows) == limit else None,
        "observations": values,
    }


@router.get("/quality")
def quality(
    token_id: str = Query(pattern=r"^[0-9]{1,120}$"),
    db: Session = Depends(get_db),
):
    require_enabled()
    now = utc_now()
    rows = history(db, token_id, now, 100)
    events = [row for row in rows if row.kind != "source_status"]
    from app.models.market_data.venue_observation import VenueObservation

    latest_status = (
        db.query(VenueObservation)
        .filter(
            VenueObservation.token_id == token_id,
            VenueObservation.kind == "source_status",
            VenueObservation.received_at <= now,
        )
        .order_by(VenueObservation.received_at.desc(), VenueObservation.id.desc())
        .first()
    )
    status = (
        serialize(latest_status)["normalized"].get("status")
        if latest_status
        else "SOURCE_STATUS_UNKNOWN"
    )
    last = events[0] if events else None
    age = max(0, (now - aware(last.received_at)).total_seconds()) if last else None
    return {
        "quality": "INSUFFICIENT_DATA",
        "source_status": status,
        "latest_receipt_at": aware(last.received_at).isoformat() if last else None,
        "age_seconds": age,
        "stale": age is None or age > 60,
        "sample_count": len(events),
        "bounded_window": 100,
        "flags": sorted({flag for row in events for flag in serialize(row)["quality"]}),
        "reason": "Capture availability does not prove continuous executable depth",
    }


def require_enabled():
    if not get_settings().observation_archive_enabled:
        raise HTTPException(503, "Observation archive disabled")


@router.get("/observations/{snapshot_id}")
def snapshot(snapshot_id: str, as_of: datetime | None = None, db: Session = Depends(get_db)):
    from app.models.market_data.venue_observation import VenueObservation

    require_enabled()
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(422, "as_of requires a timezone")
    row = db.get(VenueObservation, snapshot_id)
    if row is None or aware(row.received_at) > (as_of or utc_now()):
        raise HTTPException(404, "Observation unavailable at cutoff")
    try:
        return serialize(row)
    except ValueError as exc:
        raise HTTPException(409, "Observation integrity check failed") from exc


@router.post("/datasets")
def freeze(
    token_id: str = Query(pattern=r"^[0-9]{1,120}$"),
    as_of: datetime | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    from app.services.market_data.venue_observations import freeze_dataset

    require_enabled()
    if as_of is not None and as_of.tzinfo is None:
        raise HTTPException(422, "as_of requires a timezone")
    try:
        result = freeze_dataset(db, token_id, as_of or utc_now(), limit)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, "Dataset integrity check failed") from exc


@router.get("/datasets/{dataset_id}")
def replay(dataset_id: str, db: Session = Depends(get_db)):
    from app.services.market_data.venue_observations import replay_dataset

    require_enabled()
    try:
        return replay_dataset(db, dataset_id)
    except LookupError as exc:
        raise HTTPException(404, "Dataset unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "Dataset integrity check failed") from exc
