"""UserTrade model for tracking user's executed trades"""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class UserTrade(Base):
    """User's executed trades (copied from winners or own)"""

    __tablename__ = "user_trades"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    market_id = Column(String(200), nullable=False, index=True)
    token_id = Column(String(200), nullable=True, index=True)  # CLOB condition token
    action = Column(String(20), nullable=False)  # buy or sell
    amount = Column(Float, nullable=False)  # USDC amount
    price = Column(Float, nullable=False)  # Execution price
    status = Column(String(50), nullable=False)  # pending, executed, failed, cancelled
    order_hash = Column(String(100), nullable=True)  # Polymarket order hash
    copied_from_wallet = Column(String(42), nullable=True)  # If copied from a winner
    source_trade_history_id = Column(
        Integer,
        ForeignKey("trade_history.id"),
        nullable=True,
        index=True,
    )
    trader_trade_notional = Column(Float, nullable=True)
    trader_wallet_balance = Column(Float, nullable=True)
    copy_wallet_base = Column(Float, nullable=True)
    sizing_mode_applied = Column(String(50), nullable=True)
    copy_wallet_mode_applied = Column(String(50), nullable=True)
    calculation_warning = Column(Text, nullable=True)
    calculation_details = Column(Text, nullable=True)
    pnl = Column(Float, nullable=True)  # Profit/loss when closed
    executed_at = Column(DateTime(timezone=True), nullable=True)
    # Execution analytics (plan 03): expected vs actual fill.
    # Historical rows (pre-migration) carry NULLs — analytics
    # endpoints return null stats for those trades.
    expected_price = Column(Float, nullable=True)  # Quoted price at submission
    expected_size = Column(Float, nullable=True)  # Quoted size (USDC notional) at submission
    filled_price = Column(Float, nullable=True)  # Observed fill price
    filled_size = Column(Float, nullable=True)  # Observed filled size (shares)
    fee_paid = Column(Float, nullable=True)  # CLOB fee from the fill event (0 when absent)
    slippage_bps = Column(Float, nullable=True)  # (filled − expected) / expected × 10⁴
    latency_ms = Column(Float, nullable=True)  # filled_at − submitted_at
    strategy_source = Column(
        String(50),
        nullable=True,
    )  # copy|manual|market_maker|inverse|stop_loss|take_profit|latency_arb
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return f"<UserTrade(user={self.user_id}, market={self.market_id}, status={self.status})>"
