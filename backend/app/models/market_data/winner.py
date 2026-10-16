"""Winner model for tracking top traders"""

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.models.base import Base
from app.utils.time import utc_now


class Winner(Base):
    """Top traders on Polymarket"""

    __tablename__ = "winners"

    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String(42), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=True)
    profile_image = Column(String(500), nullable=True)

    # Core stats
    trade_count = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)  # Percentage of winning trades
    markets_traded = Column(Integer, default=0)

    # P&L by period
    pnl_24h = Column(Float, default=0.0)
    pnl_7d = Column(Float, default=0.0)
    pnl_30d = Column(Float, default=0.0)
    recent_pnl = Column(Float, default=0.0)  # Recent profit/loss in USDC
    total_pnl = Column(Float, default=0.0)  # Lifetime P&L

    # Volume & positions
    volume = Column(Float, default=0.0)
    volume_24h = Column(Float, default=0.0)
    positions_value = Column(Float, default=0.0)

    # Ranking (from Polymarket leaderboard)
    leaderboard_rank = Column(Integer, nullable=True)

    # Quality scoring (0–100 composite score)
    quality_score = Column(Float, nullable=True)  # Overall quality (0-100)
    consistency_score = Column(Float, nullable=True)  # PnL consistency across periods
    risk_adjusted_score = Column(Float, nullable=True)  # Sharpe-like risk-adjusted return
    activity_score = Column(Float, nullable=True)  # Trading activity & freshness
    quality_tier = Column(String(20), nullable=True)  # "S", "A", "B", "C", "D"
    quality_updated_at = Column(DateTime(timezone=True), nullable=True)

    last_trade_time = Column(DateTime(timezone=True), nullable=True)
    last_updated = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return (
            f"<Winner(wallet={self.wallet_address}, pnl={self.total_pnl}, "
            f"rank={self.leaderboard_rank}, tier={self.quality_tier})>"
        )
