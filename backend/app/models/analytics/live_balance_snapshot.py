"""Immutable, account-owned observations; never a spendable balance authority."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class LiveBalanceSnapshot(Base):
    __tablename__ = "live_balance_snapshots"
    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, index=True)
    captured_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    evidence_json = Column(Text, nullable=False)


class LiveAttributionReport(Base):
    """Write-once diagnostic artifact, not an authority to credit or spend money."""

    __tablename__ = "live_attribution_reports"
    id = Column(String(64), primary_key=True)
    input_hash = Column(String(64), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    report_json = Column(Text, nullable=False)


class AttributionStudy(Base):
    """Immutable, server-timed prospective evaluation policy; never order authority."""

    __tablename__ = "attribution_studies"
    id = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    registered_at = Column(DateTime(timezone=True), nullable=False)
    policy_json = Column(Text, nullable=False)
