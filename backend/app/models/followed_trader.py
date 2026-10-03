"""FollowedTrader model – tracks which top traders a user is copy-trading."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)

from app.models.base import Base
from app.utils.time import utc_now


class FollowedTrader(Base):
    """A user's subscription to copy-trade a specific top trader."""

    __tablename__ = "followed_traders"
    __table_args__ = (
        UniqueConstraint("user_id", "trader_wallet", name="uq_user_trader"),
        CheckConstraint(
            "(max_position_size IS NULL OR max_position_size >= 0)",
            name="ck_followed_traders_max_position_size_non_negative",
        ),
        CheckConstraint(
            "(fixed_trade_amount_override IS NULL OR fixed_trade_amount_override >= 0)",
            name="ck_followed_traders_fixed_override_non_negative",
        ),
        CheckConstraint(
            "(copy_wallet_percentage >= 0 AND copy_wallet_percentage <= 100)",
            name="ck_followed_traders_copy_wallet_percentage_range",
        ),
        CheckConstraint(
            "(copy_wallet_fixed_amount IS NULL OR copy_wallet_fixed_amount >= 0)",
            name="ck_followed_traders_copy_wallet_fixed_amount_non_negative",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trader_wallet = Column(String(42), nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)

    # Per-trader override (nullable = fall back to global settings)
    max_position_size = Column(Float, nullable=True)
    sizing_mode = Column(String(40), nullable=False, default="inherit_global")
    fixed_trade_amount_override = Column(Float, nullable=True)
    copy_wallet_mode = Column(
        String(50),
        nullable=False,
        default="dynamic_main_wallet_percentage",
    )
    copy_wallet_percentage = Column(Float, nullable=False, default=100.0)
    copy_wallet_fixed_amount = Column(Float, nullable=True)
    trader_alias = Column(String(100), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<FollowedTrader(user={self.user_id}, "
            f"trader={self.trader_wallet}, active={self.is_active})>"
        )
