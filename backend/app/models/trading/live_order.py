"""Durable economic identity and reservation for real exchange orders."""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)

from app.models.base import Base
from app.utils.time import utc_now


class LiveOrder(Base):
    __tablename__ = "live_orders"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_live_order_key"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, nullable=True, index=True)
    trade_id = Column(Integer, ForeignKey("user_trades.id"), nullable=False, unique=True)
    idempotency_key = Column(String(200), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    strategy = Column(String(30), nullable=False)
    token_id = Column(String(200), nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric(24, 12), nullable=False)
    shares = Column(Numeric(24, 12), nullable=False)
    notional = Column(Numeric(24, 12), nullable=False)
    # Principal stays reserved after a fill. Only an exchange-confirmed rejection
    # or cancellation without fills releases it; profits never increase budgets.
    reserved = Column(Numeric(24, 12), nullable=False)
    state = Column(String(30), nullable=False)
    order_hash = Column(String(100), nullable=True, unique=True)
    response = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
