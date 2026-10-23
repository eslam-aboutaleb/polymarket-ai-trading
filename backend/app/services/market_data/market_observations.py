"""Append/read canonical context observations using their receipt-time boundary."""

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.market_data.market_observation import MarketObservation
from app.services.intelligence.market_context import MarketContext


def append_observation(db: Session, context: MarketContext) -> None:
    """Persist an immutable response; exact retries are idempotent, never updates."""
    payload = context.model_dump_json()
    row = db.get(MarketObservation, context.snapshot_id)
    if row is not None:
        if row.payload_json != payload:
            raise ValueError("Observation identity conflicts with existing content")
        return
    db.add(
        MarketObservation(
            snapshot_id=context.snapshot_id,
            condition_id=context.condition_id,
            observed_at=context.observed_at,
            rule_hash=context.rules.content_hash,
            payload_json=payload,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.get(MarketObservation, context.snapshot_id)
        if row is None or row.payload_json != payload:
            raise


def observation_history(
    db: Session,
    condition_id: str,
    as_of: datetime,
    limit: int,
) -> list[MarketContext]:
    """Only observations already received at as_of are eligible; no look-ahead."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    rows = (
        db.query(MarketObservation)
        .filter(
            MarketObservation.condition_id == condition_id.lower(),
            MarketObservation.observed_at <= as_of.astimezone(UTC),
        )
        .order_by(MarketObservation.observed_at.desc(), MarketObservation.snapshot_id.desc())
        .limit(limit)
    )
    return [MarketContext.model_validate_json(row.payload_json) for row in rows]
