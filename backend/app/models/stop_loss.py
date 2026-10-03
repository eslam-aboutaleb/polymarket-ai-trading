"""StopLossOrder model – persistent stop-loss orders monitored in real-time."""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.utils.time import utc_now


class StopLossOrder(Base):
    """A user's stop-loss order that is monitored in the background."""

    __tablename__ = "stop_loss_orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Eager-loadable relationship (avoids N+1 wallet lookups)
    user = relationship("User", lazy="noload")
    token_id = Column(String(200), nullable=False, index=True)
    market_id = Column(String(200), nullable=False)
    market_title = Column(String(500), default="")
    outcome = Column(String(50), default="")
    size = Column(Float, nullable=False)
    stop_price = Column(Float, nullable=False)  # 0-1

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
            f"<StopLoss(id={self.id}, token={self.token_id[:20]}..., "
            f"stop={self.stop_price}, status={self.status})>"
        )
