"""Account-owned pre-trade execution estimates kept for shortfall reconciliation."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class ExecutionEstimateRecord(Base):
    __tablename__ = "execution_estimates"
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    estimate_id = Column(String(64), nullable=False)
    order_intent_id = Column(String(80), nullable=False)
    condition_id = Column(String(200), nullable=False)
    token_id = Column(String(200), nullable=False)
    side = Column(String(4), nullable=False)
    mode = Column(String(20), nullable=False)
    status = Column(String(12), nullable=False)
    shares = Column(Numeric(24, 12), nullable=False)
    book_hash = Column(String(64), nullable=True)
    arrival_mid = Column(Numeric(24, 12), nullable=True)
    benchmark_at = Column(DateTime(timezone=True), nullable=True)
    quote_expiry = Column(DateTime(timezone=True), nullable=True)
    estimate_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (
        Index("ix_execution_estimates_owner", "user_id", "account_id", "created_at"),
        Index("ix_execution_estimates_estimate", "estimate_id"),
    )
