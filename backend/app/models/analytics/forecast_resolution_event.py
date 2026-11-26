"""Append-only audit history of forecast resolution and scoring decisions (issue #60)."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, event

from app.models.base import Base
from app.utils.time import utc_now

#: What happened to a forecast's resolution.
EVENT_SCORED = "scored"
EVENT_EXCLUDED = "excluded"
EVENT_REVERSED = "reversed"
EVENT_CORRECTED = "corrected"
EVENT_REVALIDATED = "revalidated"
EVENT_TYPES = frozenset(
    {EVENT_SCORED, EVENT_EXCLUDED, EVENT_REVERSED, EVENT_CORRECTED, EVENT_REVALIDATED}
)


class ForecastResolutionEvent(Base):
    """One immutable fact about how a forecast was (or was not) scored."""

    __tablename__ = "forecast_resolution_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    assessment_id = Column(Integer, ForeignKey("assessments.id"), nullable=False, index=True)
    condition_id = Column(String(100), nullable=True)
    event_type = Column(String(20), nullable=False)
    scoring_version = Column(String(32), nullable=True)
    resolved_outcome = Column(String(20), nullable=True)
    brier_score = Column(String(32), nullable=True)
    exclusion_reason = Column(String(40), nullable=True)
    #: Finality evidence: Gamma's UMA status string at the time of the event.
    finality_status = Column(String(32), nullable=True)
    rule_hash = Column(String(64), nullable=True)
    detail_json = Column(Text, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (Index("ix_forecast_resolution_events_condition", "condition_id"),)


class AppendOnlyAuditError(RuntimeError):
    """Raised on any attempt to edit or delete an audit event."""


@event.listens_for(ForecastResolutionEvent, "before_update")
def _no_update(_mapper, _connection, _target):  # pragma: no cover - exercised in tests
    raise AppendOnlyAuditError("forecast resolution events are append-only")


@event.listens_for(ForecastResolutionEvent, "before_delete")
def _no_delete(_mapper, _connection, _target):  # pragma: no cover - exercised in tests
    raise AppendOnlyAuditError("forecast resolution events are append-only")
