"""Desired maker quote definitions, explicitly not resting orders or fills."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.models.base import Base
from app.utils.time import utc_now


class PaperMakerIntent(Base):
    __tablename__ = "paper_maker_intents"
    id = Column(String(36), primary_key=True)
    config_id = Column(Integer, ForeignKey("market_maker_configs.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, nullable=True)
    quote_key = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    state = Column(String(40), nullable=False, default="UNFILLED_DATA_MISSING")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    __table_args__ = (UniqueConstraint("config_id", "quote_key", name="uq_paper_maker_quote"),)
