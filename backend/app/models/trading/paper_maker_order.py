"""Durable modeled maker lifecycle and evidence; separate from live orders."""

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from app.models.base import Base
from app.utils.time import utc_now


class PaperMakerOrder(Base):
    __tablename__ = "paper_maker_orders"
    id = Column(String(36), primary_key=True)
    config_id = Column(Integer, ForeignKey("market_maker_configs.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    quote_key = Column(String(64), nullable=False)
    source_snapshot_id = Column(String(64), ForeignKey("venue_observations.id"), nullable=False)
    metadata_snapshot_id = Column(String(64), ForeignKey("venue_observations.id"), nullable=False)
    payload_json = Column(Text, nullable=False)
    state = Column(String(40), nullable=False)
    shares = Column(Numeric(28, 8), nullable=False)
    limit_price = Column(Numeric(28, 8), nullable=False)
    cumulative_filled = Column(Numeric(28, 8), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class PaperMakerCursor(Base):
    __tablename__ = "paper_maker_cursors"
    account_id = Column(Integer, ForeignKey("accounts.id"), primary_key=True)
    available_at = Column(DateTime(timezone=True), nullable=True)


class PaperMakerEvidence(Base):
    __tablename__ = "paper_maker_evidence"
    account_id = Column(Integer, ForeignKey("accounts.id"), primary_key=True)
    event_id = Column(String(200), primary_key=True)
    payload_hash = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)


class PaperMakerFill(Base):
    __tablename__ = "paper_maker_fills"
    id = Column(String(36), primary_key=True)
    order_id = Column(String(36), ForeignKey("paper_maker_orders.id"), nullable=False, index=True)
    event_id = Column(String(200), nullable=False)
    fill_sequence = Column(Integer, nullable=False, default=0)
    realized_pnl_usdc = Column(Text, nullable=False)
    payload_json = Column(Text, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("order_id", "event_id", "fill_sequence", name="uq_maker_fill_event"),
    )


class PaperMakerMark(Base):
    __tablename__ = "paper_maker_marks"
    fill_id = Column(String(36), ForeignKey("paper_maker_fills.id"), primary_key=True)
    horizon_seconds = Column(Integer, primary_key=True)
    payload_json = Column(Text, nullable=False)


class PaperMakerLegacy(Base):
    __tablename__ = "paper_maker_legacy"
    trade_id = Column(Integer, ForeignKey("user_trades.id"), primary_key=True)
    quality = Column(String(40), nullable=False, default="legacy_unverified_fill")
    original_hash = Column(String(64), nullable=False)
