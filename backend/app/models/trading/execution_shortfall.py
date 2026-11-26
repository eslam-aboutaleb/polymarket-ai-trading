"""Per-decision implementation shortfall and fill-rate record (issue #157)."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.models.base import Base
from app.utils.time import utc_now


class ExecutionShortfall(Base):
    __tablename__ = "execution_shortfalls"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, nullable=True, index=True)
    strategy = Column(String(40), nullable=False)
    mode = Column(String(20), nullable=False)  # "taker" | "limit_then_take"
    decision_id = Column(String(120), nullable=False)
    token_id = Column(String(200), nullable=False)
    side = Column(String(4), nullable=False)
    outcome = Column(String(40), nullable=False)
    shares_requested = Column(Numeric(38, 18), nullable=False)
    shares_filled = Column(Numeric(38, 18), nullable=False)
    maker_shares = Column(Numeric(38, 18), nullable=False, default=0)
    decision_price = Column(Numeric(38, 18), nullable=False)
    avg_fill_price = Column(Numeric(38, 18), nullable=True)
    fees_usdc = Column(Numeric(38, 18), nullable=False, default=0)
    # Cost vs the decision price, signed so positive is worse for the trader.
    shortfall_usdc = Column(Numeric(38, 18), nullable=True)
    missed_shares = Column(Numeric(38, 18), nullable=False, default=0)
    missed_cost_usdc = Column(Numeric(38, 18), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (
        UniqueConstraint("user_id", "strategy", "decision_id", name="uq_exec_shortfall_decision"),
    )
