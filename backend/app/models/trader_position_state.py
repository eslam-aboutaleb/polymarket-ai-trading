"""Net position tracker per source trader wallet/token."""
from datetime import datetime
from app.utils.time import utc_now

from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint

from app.models.base import Base


class TraderPositionState(Base):
    """Tracks running net size for classifying opened/closed events."""

    __tablename__ = "trader_position_state"
    __table_args__ = (
        UniqueConstraint(
            "trader_wallet",
            "token_id",
            name="uq_trader_wallet_token",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    trader_wallet = Column(String(42), nullable=False, index=True)
    token_id = Column(String(200), nullable=False, index=True)
    market_id = Column(String(200), nullable=False, default="")
    net_size = Column(Float, nullable=False, default=0.0)
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    def __repr__(self):
        return (
            "<TraderPositionState("
            f"trader={self.trader_wallet}, token={self.token_id[:14]}..., net={self.net_size})>"
        )
