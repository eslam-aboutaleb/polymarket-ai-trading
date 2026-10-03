"""Backtesting models for strategy simulation results."""

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.models.base import Base
from app.utils.time import utc_now


class BacktestRun(Base):
    """A single backtest run with its parameters and results."""

    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Strategy being tested: "copy_trade" | "inverse_bot" | "market_maker" | "custom"
    strategy_type = Column(String(50), nullable=False, index=True)
    strategy_name = Column(String(200), nullable=False, default="")

    # Time range
    start_date = Column(String(30), nullable=False)
    end_date = Column(String(30), nullable=False)

    # Parameters (JSON blob of strategy-specific config)
    parameters = Column(JSON, nullable=False, default=dict)

    # Results
    total_trades = Column(Integer, nullable=False, default=0)
    winning_trades = Column(Integer, nullable=False, default=0)
    losing_trades = Column(Integer, nullable=False, default=0)
    win_rate = Column(Float, nullable=True)
    total_pnl = Column(Float, nullable=False, default=0.0)
    max_drawdown = Column(Float, nullable=True)
    sharpe_ratio = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    avg_trade_pnl = Column(Float, nullable=True)
    max_consecutive_losses = Column(Integer, nullable=True)
    total_volume = Column(Float, nullable=True)

    # Status: "pending" | "running" | "completed" | "failed"
    status = Column(String(30), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)

    # Full trade log (JSON array)
    trade_log = Column(JSON, nullable=True)

    # Technical indicator values used (JSON)
    indicator_values = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return (
            f"<BacktestRun(id={self.id}, strategy={self.strategy_type}, "
            f"status={self.status}, pnl={self.total_pnl})>"
        )
