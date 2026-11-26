"""Immutable evidence snapshot behind a generated forecast (issue #4).

Content-addressed: ``snapshot_id`` is the hash ``forecast_generation`` computes over exactly
the usable evidence, and ``Assessment.evidence_snapshot_id`` points here. It is evidence
storage, not a second forecasts table.
"""

from sqlalchemy import Column, DateTime, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class ForecastEvidenceSnapshot(Base):
    __tablename__ = "forecast_evidence_snapshots"

    snapshot_id = Column(String(64), primary_key=True)
    payload_json = Column(Text, nullable=False)
    as_of = Column(DateTime(timezone=True), nullable=False)
    generator_version = Column(String(40), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
