"""Durable account-scoped simulation state; never represents actual wallet cash."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from app.models.base import Base
from app.utils.time import utc_now


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    account_id = Column(Integer, ForeignKey("accounts.id"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    funding = Column(Numeric(28, 8), nullable=False)
    fee_bps = Column(Numeric(13, 8), nullable=True)
    legacy_unverified = Column(Boolean, nullable=False, default=False)
    legacy_acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    policy_json = Column(Text, nullable=False, default="{}")
    started_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class PaperJournalEvent(Base):
    __tablename__ = "paper_journal_events"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_id = Column(String(200), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    payload_json = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("account_id", "event_id", name="uq_paper_journal_event"),)


class PaperOrderRecord(Base):
    __tablename__ = "paper_order_records"
    id = Column(String(36), primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    intent_key = Column(String(200), nullable=False)
    journal_sequence = Column(Integer, nullable=False, default=0)
    request_hash = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("account_id", "intent_key", name="uq_paper_order_intent"),)


class PaperNavPoint(Base):
    __tablename__ = "paper_nav_points"
    id = Column(String(36), primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    bucket_hour = Column(DateTime(timezone=True), nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    valuation_json = Column(Text, nullable=False)
    __table_args__ = (
        Index("ix_paper_nav_account_clock", "account_id", "captured_at"),
        UniqueConstraint("account_id", "bucket_hour", name="uq_paper_nav_account_bucket"),
    )
