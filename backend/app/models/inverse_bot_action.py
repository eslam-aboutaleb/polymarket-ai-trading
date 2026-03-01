"""Audit log for inverse-bot reversal attempts and outcomes."""
from datetime import datetime
from app.utils.time import utc_now

from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey, Text

from app.models.base import Base


class InverseBotAction(Base):
    """Stores each reversal decision and execution outcome."""

    __tablename__ = "inverse_bot_actions"

    id = Column(Integer, primary_key=True, index=True)
    inverse_bot_position_id = Column(
        Integer, ForeignKey("inverse_bot_positions.id"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    condition_id = Column(String(200), nullable=False, index=True)

    from_token_id = Column(String(200), nullable=False)
    to_token_id = Column(String(200), nullable=True)
    from_outcome = Column(String(100), nullable=True)
    to_outcome = Column(String(100), nullable=True)

    sell_order_hash = Column(String(200), nullable=True)
    buy_order_hash = Column(String(200), nullable=True)
    sell_size = Column(Float, nullable=True)
    buy_notional = Column(Float, nullable=True)

    confidence = Column(Float, nullable=True)
    recommendation = Column(String(30), nullable=True)
    # "success" | "failed" | "sell_only" | "skipped"
    status = Column(String(30), nullable=False, default="failed")
    error = Column(Text, nullable=True)

    executed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<InverseBotAction(user_id={self.user_id}, condition_id={self.condition_id[:12]}..., "
            f"status={self.status})>"
        )
