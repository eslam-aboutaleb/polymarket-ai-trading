"""Persistence adapters for trading workflows.

Trading services own use-case decisions; these repositories own ORM query
construction and return mapped rows or scalar projections to their callers.
"""

from app.repositories.trading.backtesting import TradingBacktestRepository
from app.repositories.trading.bots import TradingBotRepository
from app.repositories.trading.follows import TradingFollowRepository
from app.repositories.trading.orders import (
    StopLossRepository,
    TakeProfitRepository,
    TradingOrderRepository,
    UserTradeRepository,
)
from app.repositories.trading.redemptions import TradingRedemptionRepository

__all__ = [
    "TradingBacktestRepository",
    "TradingBotRepository",
    "TradingFollowRepository",
    "TradingOrderRepository",
    "TradingRedemptionRepository",
    "StopLossRepository",
    "TakeProfitRepository",
    "UserTradeRepository",
]
