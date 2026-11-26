"""Append-only resolution/scoring audit writer (issue #60)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.analytics.assessment import Assessment
from app.models.analytics.forecast_resolution_event import (
    EVENT_TYPES,
    ForecastResolutionEvent,
)


def append_resolution_event(
    db: Session,
    row: Assessment,
    event_type: str,
    *,
    scoring_version: str | None = None,
    resolved_outcome: str | None = None,
    brier_score: float | None = None,
    exclusion_reason: str | None = None,
    finality_status: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ForecastResolutionEvent:
    """Add one immutable event for ``row``. The caller commits."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown resolution event type: {event_type}")
    event = ForecastResolutionEvent(
        assessment_id=row.id,
        condition_id=row.condition_id,
        event_type=event_type,
        scoring_version=scoring_version,
        resolved_outcome=resolved_outcome,
        brier_score=None if brier_score is None else repr(float(brier_score)),
        exclusion_reason=exclusion_reason,
        finality_status=finality_status,
        rule_hash=row.rule_hash,
        detail_json=json.dumps(detail, sort_keys=True, default=str) if detail else None,
    )
    db.add(event)
    return event


def resolution_history(db: Session, assessment_id: int) -> list[ForecastResolutionEvent]:
    """Events for one forecast, oldest first."""
    return (
        db.query(ForecastResolutionEvent)
        .filter(ForecastResolutionEvent.assessment_id == assessment_id)
        .order_by(ForecastResolutionEvent.id)
        .all()
    )
