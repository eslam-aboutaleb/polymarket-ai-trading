"""TradeHistory model for tracking trades"""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from app.models.base import Base
from app.utils.time import utc_now


class TradeHistory(Base):
    """Historical trade record from top traders"""

    __tablename__ = "trade_history"

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(100), ForeignKey("markets.id"), nullable=False, index=True)
    wallet_address = Column(String(42), nullable=False, index=True)
    order_type = Column(String(20), nullable=False)  # buy or sell
    amount = Column(Float, nullable=False)  # USDC amount
    price = Column(Float, nullable=False)  # Price paid
    notional_usdc = Column(Float, nullable=True)
    source_trade_id_ext = Column(String(120), nullable=True, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<TradeHistory(market={self.market_id}, type={self.order_type}, amount={self.amount})>"
        )
