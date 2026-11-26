"""Frozen, account-owned allocation simulations (advisory; never an order)."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class PortfolioScenario(Base):
    __tablename__ = "portfolio_scenarios"
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    input_json = Column(Text, nullable=False)
    input_hash = Column(String(64), nullable=False)
    proposal_json = Column(Text, nullable=False)
    allocator_version = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (Index("ix_portfolio_scenarios_owner", "user_id", "account_id"),)
