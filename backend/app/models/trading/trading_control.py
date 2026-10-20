"""Durable halt and cancellation work, independent of process-local caches."""

import uuid

from sqlalchemy import JSON, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String

from app.models.base import Base
from app.utils.time import utc_now


class TradingControlState(Base):
    __tablename__ = "trading_control_states"

    scope = Column(String(80), primary_key=True)  # global or user:<id> (wallet scope)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    state = Column(String(30), nullable=False, default="RECOVERY_REQUIRED")
    reason = Column(String(500), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "state IN ('RUNNING','HALTING','HALTED','RECOVERY_REQUIRED')",
            name="ck_trading_control_state",
        ),
    )


class EmergencyHaltOperation(Base):
    __tablename__ = "emergency_halt_operations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    state = Column(String(30), nullable=False, default="PENDING", index=True)
    close_positions = Column(Boolean, nullable=False, default=False)
    control_version = Column(Integer, nullable=False)
    # This workflow cancels orders only. Position exits need the future gateway.
    report = Column(JSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING','VERIFIED','RECOVERY_REQUIRED')",
            name="ck_emergency_operation_state",
        ),
    )
