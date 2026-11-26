"""Immutable record of one structured Market Deep Dive (#33 Release D).

Rows are insert-only. They reference evidence, forecast and snapshot identities
instead of copying the forecast or calibration stores, and carry no prompt text,
raw model output, credentials or wallet data. Access is scoped to the owner.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (Index("ix_analysis_runs_user_created", "user_id", "created_at"),)

    analysis_id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    condition_id = Column(String(66), nullable=False, index=True)
    selected_token_id = Column(String(120), nullable=True)
    snapshot_id = Column(String(64), nullable=True)
    rules_hash = Column(String(64), nullable=False)
    prompt_version = Column(String(16), nullable=True)
    provider = Column(String(50), nullable=True)
    model = Column(String(100), nullable=True)
    forecast_id = Column(Integer, ForeignKey("assessments.id"), nullable=True, index=True)
    # Probabilities at analysis time, stored as decimal strings for later paired scoring.
    forecast_p_yes = Column(String(40), nullable=True)
    market_p_yes = Column(String(40), nullable=True)
    evidence_refs_json = Column(Text, nullable=False)
    decision = Column(String(20), nullable=False)
    reason_codes_json = Column(Text, nullable=False)
    completeness_json = Column(Text, nullable=False)
    result_sha256 = Column(String(64), nullable=False)
    pivotal_claims = Column(Integer, nullable=False, default=0)
    unsupported_pivotal_claims = Column(Integer, nullable=False, default=0)
    attributed_claims = Column(Integer, nullable=False, default=0)
    total_claims = Column(Integer, nullable=False, default=0)
    first_fact_ms = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False)  # completed | partial_failure
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
