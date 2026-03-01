"""Winner model for tracking top traders"""
from sqlalchemy import Column, String, Float, DateTime, Integer, Text
from datetime import datetime
from app.utils.time import utc_now
from app.models.base import Base


class Winner(Base):
    """Top traders on Polymarket"""
    __tablename__ = "winners"
    
    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String(42), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=True)
    profile_image = Column(String(500), nullable=True)
    
    # Core stats
    trade_count = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)       # Percentage of winning trades
    markets_traded = Column(Integer, default=0)
    
    # P&L by period
    pnl_24h = Column(Float, default=0.0)
    pnl_7d = Column(Float, default=0.0)
    pnl_30d = Column(Float, default=0.0)
    recent_pnl = Column(Float, default=0.0)     # Recent profit/loss in USDC
    total_pnl = Column(Float, default=0.0)      # Lifetime P&L
    
    # Volume & positions
    volume = Column(Float, default=0.0)
    volume_24h = Column(Float, default=0.0)
    positions_value = Column(Float, default=0.0)
    
    # Ranking (from Polymarket leaderboard)
    leaderboard_rank = Column(Integer, nullable=True)
    
    last_trade_time = Column(DateTime(timezone=True), nullable=True)
    last_updated = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    
    def __repr__(self):
        return f"<Winner(wallet={self.wallet_address}, pnl={self.total_pnl}, rank={self.leaderboard_rank})>"
