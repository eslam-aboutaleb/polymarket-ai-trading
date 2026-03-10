"""TakeProfitOrder model – persistent take-profit orders monitored in real-time."""
from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey
from sqlalchemy.orm import relationship
from app.utils.time import utc_now
from app.models.base import Base


class TakeProfitOrder(Base):
    """A user's take-profit order that is monitored in the background."""

    __tablename__ = "take_profit_orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Eager-loadable relationship (avoids N+1 wallet lookups)
    user = relationship("User", lazy="noload")
    token_id = Column(String(200), nullable=False, index=True)
    market_id = Column(String(200), nullable=False)
    market_title = Column(String(500), default="")
    outcome = Column(String(50), default="")
    size = Column(Float, nullable=False)
    take_profit_price = Column(Float, nullable=False)  # 0-1, triggers when price >= this

    # Status: active | triggered | cancelled | failed
    status = Column(String(30), nullable=False, default="active", index=True)

    # Execution details (filled when triggered)
    order_hash = Column(String(200), nullable=True)
    executed_price = Column(Float, nullable=True)
    triggered_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return (
            f"<TakeProfit(id={self.id}, token={self.token_id[:20]}..., "
            f"tp={self.take_profit_price}, status={self.status})>"
        )
