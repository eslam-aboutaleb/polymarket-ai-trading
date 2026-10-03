"""Inverse bot position configuration per user-held token."""

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.models.base import Base
from app.utils.time import utc_now


class InverseBotPosition(Base):
    """Tracks inverse-bot state and overrides for a held position."""

    __tablename__ = "inverse_bot_positions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    token_id = Column(String(200), nullable=False, index=True)
    condition_id = Column(String(200), nullable=False, index=True)
    market_title = Column(String(500), default="")
    outcome = Column(String(100), default="")
    enabled = Column(Boolean, nullable=False, default=True, index=True)

    # "inherit" | "full_notional" | "fixed_amount"
    size_mode_override = Column(String(20), nullable=False, default="inherit")
    fixed_amount_override = Column(Float, nullable=True)

    # "active" | "cooldown" | "sell_only" | "error"
    status = Column(String(30), nullable=False, default="active")

    last_signal = Column(String(30), nullable=True)
    last_confidence = Column(Float, nullable=True)
    last_reasoning = Column(Text, nullable=True)
    last_web_summary = Column(Text, nullable=True)
    last_x_summary = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    last_recommendation = Column(String(30), nullable=True)
    last_alt_outcome = Column(String(100), nullable=True)
    last_alt_token_id = Column(String(200), nullable=True)

    last_evaluated_at = Column(DateTime(timezone=True), nullable=True)
    last_reversed_at = Column(DateTime(timezone=True), nullable=True)

    reversals_today = Column(Integer, nullable=False, default=0)
    reversals_day = Column(Date, nullable=True)
    persistence_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return (
            f"<InverseBotPosition(user_id={self.user_id}, token_id={self.token_id[:12]}..., "
            f"enabled={self.enabled}, status={self.status})>"
        )
