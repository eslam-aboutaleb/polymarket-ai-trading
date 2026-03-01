"""Assessment model for AI trade evaluation"""
from sqlalchemy import Column, String, Float, DateTime, Integer, ForeignKey, Text
from datetime import datetime
from app.utils.time import utc_now
from app.models.base import Base


class Assessment(Base):
    """AI assessment of a trade opportunity"""
    __tablename__ = "assessments"
    
    id = Column(Integer, primary_key=True, index=True)
    trade_history_id = Column(Integer, ForeignKey("trade_history.id"), nullable=False, index=True)
    ai_score = Column(Float, nullable=False)  # 0-100 confidence score
    reasoning = Column(Text, nullable=False)
    recommendation = Column(String(50), nullable=False)  # copy, avoid, hold, etc.
    risk_level = Column(String(20), nullable=False)  # low, medium, high
    market_sentiment = Column(String(50), nullable=True)  # bullish, bearish, neutral
    confidence = Column(Float, nullable=False)  # 0-100
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    
    def __repr__(self):
        return f"<Assessment(trade_id={self.trade_history_id}, score={self.ai_score})>"
