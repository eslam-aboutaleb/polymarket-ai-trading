"""Per-user whale monitoring configuration."""

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer

from app.models.base import Base
from app.utils.time import utc_now


class WhaleConfig(Base):
    """Whale-monitoring thresholds and watchlist for a single user."""

    __tablename__ = "whale_configs"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    min_notional = Column(Float, nullable=False, default=10000.0)
    auto_copy = Column(Boolean, nullable=False, default=False)
    watchlist = Column(JSON, nullable=False, default=list)
    whale_set_size = Column(Integer, nullable=False, default=50)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<WhaleConfig(user={self.user_id}, min_notional={self.min_notional}, "
            f"auto_copy={self.auto_copy})>"
        )
