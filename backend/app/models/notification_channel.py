"""Notification channel model for multi-channel alerting.

A channel is a user-configured delivery target (Telegram chat, Discord
webhook, or generic HTTPS webhook). Channel-specific configuration
(chat_id, webhook URL, signing secret) is stored in ``config_json``
encrypted at rest with Fernet via ``app.security.crypto``.
"""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now

CHANNEL_TYPES = ("telegram", "discord", "webhook")


class NotificationChannel(Base):
    """A user-configured alert delivery channel."""

    __tablename__ = "notification_channels"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    channel_type = Column(String(20), nullable=False)  # telegram | discord | webhook
    name = Column(String(100), nullable=False, default="")
    # Fernet-encrypted JSON blob; plaintext never touches the database.
    config_json = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return (
            "<NotificationChannel("
            f"user={self.user_id}, type={self.channel_type}, active={self.is_active})>"
        )
