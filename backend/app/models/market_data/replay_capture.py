"""Immutable public venue observations and first-seen trader signals."""

from sqlalchemy import Column, DateTime, Index, String, Text

from app.models.base import Base


class ReplayCapture(Base):
    __tablename__ = "replay_captures"
    id = Column(String(64), primary_key=True)
    kind = Column(String(20), nullable=False)
    source_key = Column(String(200), nullable=False)
    condition_id = Column(String(66), nullable=True)
    token_id = Column(String(200), nullable=True)
    wallet = Column(String(42), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    source_at = Column(DateTime(timezone=True), nullable=True)
    content_hash = Column(String(64), nullable=False)
    rule_hash = Column(String(64), nullable=True)
    payload_json = Column(Text, nullable=False)
    __table_args__ = (
        Index("ix_replay_capture_token_clock", "token_id", "observed_at"),
        Index("ix_replay_capture_wallet_clock", "wallet", "observed_at"),
    )
