"""Per-user notification preference model for event routing."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.models.base import Base
from app.utils.time import utc_now


class UserNotificationPreference(Base):
    """Per-user routing rules: which channels fire for which event types.

    ``channel_ids`` is a JSON array of notification_channels.id values.
    A missing preference row falls back to all of the user's active
    channels; a row with ``enabled=False`` silences the event type.
    """

    __tablename__ = "user_notification_preferences"
    __table_args__ = (UniqueConstraint("user_id", "event_type", name="uq_user_event_pref"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event_type = Column(String(length=40), nullable=False)
    channel_ids = Column(Text, nullable=False, default="[]")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
