"""Notification delivery model tracking per-channel alert dispatch attempts."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now

DELIVERY_STATUSES = ("pending", "sent", "failed")


class NotificationDelivery(Base):
    """A single attempt trail for delivering one alert to one channel."""

    __tablename__ = "notification_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(
        Integer,
        ForeignKey("notification_channels.id"),
        nullable=False,
        index=True,
    )
    event_id = Column(String(100), nullable=True, index=True)
    event_type = Column(String(40), nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    status = Column(String(20), nullable=False, default="pending")
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return (
            "<NotificationDelivery("
            f"channel={self.channel_id}, type={self.event_type}, "
            f"status={self.status}, attempts={self.attempts})>"
        )
