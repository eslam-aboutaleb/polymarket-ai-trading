"""Immutable account-scoped input/output archive for user-supplied research runs."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class FollowerExperimentRun(Base):
    __tablename__ = "follower_experiment_runs"

    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    input_hash = Column(String(64), nullable=False)
    input_json = Column(Text, nullable=False)
    result_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (Index("ix_follower_runs_owner_account", "user_id", "account_id"),)
