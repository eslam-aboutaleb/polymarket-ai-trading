"""Verdict feedback model (plan 04).

A one-to-five-star rating a trader attaches to an AI verdict. It is the
human-in-the-loop signal: it does not retrain anything on its own (no
online learning here), but it is what a prompt author looks at to judge
whether the last prompt change helped.

Feedback is deliberately not written back onto ``assessment``: a
feedback row has its own lifecycle (it can arrive days after the market
resolved) and its own write path, and a DELETE/UPDATE of a rating must
not touch the calibration sample.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)

from app.models.base import Base
from app.utils.time import utc_now


class VerdictFeedback(Base):
    __tablename__ = "verdict_feedback"

    id = Column(Integer, primary_key=True, index=True)
    # The verdict being rated. Deleting an assessment is an operational
    # action, so the FK is left unconstrained rather than cascading —
    # feedback survives as an audit record.
    assessment_id = Column(Integer, ForeignKey("assessments.id"), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=True, index=True)
    rating = Column(SmallInteger, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)

    # The bounds live in the schema, not only in the route validator, so
    # a backfill or a seed script cannot write a rating out of range.
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_verdict_feedback_rating"),
    )

    def __repr__(self):
        return f"<VerdictFeedback(assessment={self.assessment_id}, rating={self.rating})>"


class LLMExperiment(Base):
    """A/B experiment over model configuration for one task (plan 04).

    Traffic splitting is deterministic and stateless:
    ``sha256(user_id + name) % 100 < traffic_pct`` picks the variant, so
    it is reproducible in tests and survives a restart with no sticky
    session store. Only one experiment may be active per task — enforced
    by a unique partial index in the migration.
    """

    __tablename__ = "llm_experiments"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), nullable=False)
    # Task key from the shared TASK_TIERS table, e.g. "market_analysis".
    task = Column(String(50), nullable=False, index=True)
    # Control/variant config dicts, in the same shape ``resolve_tier_config``
    # returns.
    control_config_json = Column(Text, nullable=True)
    variant_config_json = Column(Text, nullable=True)
    # Percentage of users routed to the variant (0-100).
    traffic_pct = Column(Integer, nullable=False, default=10)
    status = Column(String(20), nullable=False, default="active")  # active | paused | done
    created_by = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    # A unique *index* rather than a column-level ``unique=True``: it is
    # the portable spelling across backends (SQLite cannot ADD a UNIQUE
    # constraint), and the migration creates exactly this index so
    # ``alembic compare_metadata`` stays drift-free.
    __table_args__ = (Index("uq_llm_experiments_name", "name", unique=True),)

    def __repr__(self):
        return f"<LLMExperiment({self.name}, task={self.task}, pct={self.traffic_pct})>"
