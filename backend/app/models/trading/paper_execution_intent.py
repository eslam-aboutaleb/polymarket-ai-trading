"""Immutable pre-capture intent; monetary outcomes belong to the shared journal."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.models.base import Base
from app.utils.time import utc_now


class PaperExecutionIntent(Base):
    __tablename__ = "paper_execution_intents"
    id = Column(String(36), primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    intent_key = Column(String(200), nullable=False)
    request_hash = Column(String(64), nullable=False)
    request_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("account_id", "intent_key", name="uq_execution_intent"),)
