"""Market model for Polymarket data"""

from sqlalchemy import Column, DateTime, Float, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class Market(Base):
    """Market data from Polymarket"""

    __tablename__ = "markets"

    id = Column(String(100), primary_key=True, index=True)  # Market ID from Polymarket
    question = Column(String(500), nullable=False)
    current_price = Column(Float, nullable=True)  # Current market probability
    liquidity = Column(Float, nullable=True)  # Total liquidity
    volume = Column(Float, default=0.0)
    status = Column(String(50), nullable=False)  # active, closed, resolved, etc.
    description = Column(Text, nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f"<Market(id={self.id}, price={self.current_price})>"
