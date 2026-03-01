"""Notification feed event model for followed trader activity."""
from datetime import datetime
from app.utils.time import utc_now

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.models.base import Base


class NotificationFeedEvent(Base):
    """A notification event visible in dashboard feed and optionally emailed."""

    __tablename__ = "notification_feed_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trader_wallet = Column(String(42), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)  # opened | closed
    market_id = Column(String(200), nullable=False, default="")
    token_id = Column(String(200), nullable=False, default="")
    side = Column(String(20), nullable=False, default="")
    size = Column(Float, nullable=False, default=0.0)
    price = Column(Float, nullable=False, default=0.0)
    prev_net_size = Column(Float, nullable=False, default=0.0)
    new_net_size = Column(Float, nullable=False, default=0.0)
    source_trade_history_id = Column(
        Integer,
        ForeignKey("trade_history.id"),
        nullable=True,
        index=True,
    )
    email_status = Column(
        String(20),
        nullable=False,
        default="pending",  # pending | sent | failed | skipped
    )
    email_error = Column(Text, nullable=True)
    emailed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return (
            "<NotificationFeedEvent("
            f"user={self.user_id}, trader={self.trader_wallet}, type={self.event_type})>"
        )
