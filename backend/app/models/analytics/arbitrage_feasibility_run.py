"""Frozen, account-owned arbitrage feasibility assessments (paper research only)."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class ArbitrageFeasibilityRun(Base):
    __tablename__ = "arbitrage_feasibility_runs"
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    research_run_id = Column(String(36), ForeignKey("book_research_runs.id"), nullable=False)
    candidate_id = Column(String(64), nullable=False)
    input_json = Column(Text, nullable=False)
    input_hash = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    result_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (Index("ix_arb_feasibility_owner", "user_id", "account_id"),)
