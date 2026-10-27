"""Private, account-owned analytical recipes; public receipts remain separate."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class BookResearchRun(Base):
    __tablename__ = "book_research_runs"
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    dataset_id = Column(String(64), ForeignKey("observation_datasets.id"), nullable=False)
    input_json = Column(Text, nullable=False)
    input_hash = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    result_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (Index("ix_book_research_owner", "user_id", "account_id"),)
