"""Point-in-time, hash-versioned resolution rule snapshots for audit and invalidation."""

from sqlalchemy import Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from app.models.base import Base
from app.utils.time import utc_now


class ResolutionRuleSnapshot(Base):
    """Immutable point-in-time capture of a market's resolution rule specification."""

    __tablename__ = "resolution_rule_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    condition_id = Column(String(66), nullable=False)
    source_hash = Column(String(64), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    source_uri = Column(String(512), nullable=False)
    review_status = Column(String(32), nullable=False)
    token_outcomes_json = Column(Text, nullable=False)
    spec_json = Column(Text, nullable=False)
    ambiguity_flags_json = Column(Text, nullable=False)
    abstain_reasons_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint(
            "condition_id", "source_hash", "observed_at", name="uq_resolution_rule_capture"
        ),
        Index("ix_resolution_rule_condition_observed", "condition_id", "observed_at"),
    )
