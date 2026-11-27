"""Durable redemption identity and wallet nonce exclusion; never store signed bytes."""

from sqlalchemy import (
    Boolean,
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


class RedemptionIntent(Base):
    __tablename__ = "redemption_intents"
    __table_args__ = (
        UniqueConstraint("account_id", "idempotency_key", name="uq_redemption_intent_key"),
        UniqueConstraint("chain_id", "wallet", "nonce", name="uq_redemption_wallet_nonce"),
    )

    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    idempotency_key = Column(String(160), nullable=False)
    request_hash = Column(String(64), nullable=False)
    execution_mode = Column(String(12), nullable=False)
    chain_id = Column(Integer, nullable=False)
    wallet = Column(String(42), nullable=False)
    condition_id = Column(String(66), nullable=False)
    snapshot_json = Column(Text, nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    state = Column(String(24), nullable=False)
    nonce = Column(Integer, nullable=True)
    transaction_json = Column(Text, nullable=True)
    transaction_hash = Column(String(66), nullable=True, unique=True)
    receipt_json = Column(Text, nullable=True)
    receipt_hash = Column(String(64), nullable=True)
    payout = Column(Numeric(36, 6), nullable=True)
    finalized_once = Column(Boolean, default=False, nullable=False)
    reason = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class RedemptionWalletLease(Base):
    __tablename__ = "redemption_wallet_leases"
    chain_id = Column(Integer, primary_key=True)
    wallet = Column(String(42), primary_key=True)
    active_intent_id = Column(String(64), ForeignKey("redemption_intents.id"), nullable=True)


class RedemptionObservation(Base):
    __tablename__ = "redemption_observations"
    id = Column(String(64), primary_key=True)
    intent_id = Column(String(64), ForeignKey("redemption_intents.id"), nullable=False, index=True)
    proof_hash = Column(String(64), nullable=False)
    proof_json = Column(Text, nullable=False)
    observed_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
