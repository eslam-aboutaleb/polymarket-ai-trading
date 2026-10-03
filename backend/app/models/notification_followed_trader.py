"""Per-user trader notification follow configuration."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)

from app.models.base import Base
from app.utils.time import utc_now


class NotificationFollowedTrader(Base):
    """Tracks which trader wallets a user follows for notifications."""

    __tablename__ = "notification_followed_traders"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "trader_wallet",
            name="uq_notification_user_trader",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trader_wallet = Column(String(42), nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    feed_enabled = Column(Boolean, default=True, nullable=False)
    email_enabled = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    def __repr__(self):
        return (
            "<NotificationFollowedTrader("
            f"user={self.user_id}, trader={self.trader_wallet}, active={self.is_active})>"
        )
