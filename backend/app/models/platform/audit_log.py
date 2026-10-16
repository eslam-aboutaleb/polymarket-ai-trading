"""Audit models for privileged admin state transitions."""

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.models.base import Base
from app.utils.time import utc_now


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String(42), nullable=False, index=True)
    action = Column(String(20), nullable=False)  # promoted, demoted
    source = Column(String(50), nullable=False, default="env_sync")
    performed_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    previous_state = Column(Boolean, nullable=False)
    new_state = Column(Boolean, nullable=False)
