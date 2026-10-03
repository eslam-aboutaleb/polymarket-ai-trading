"""SQLAlchemy models – import all so Base.metadata knows every table."""

from app.models.assessment import Assessment
from app.models.audit_log import AdminAuditLog
from app.models.backtest_run import BacktestRun
from app.models.base import Base
from app.models.followed_trader import FollowedTrader
from app.models.inverse_bot_action import InverseBotAction
from app.models.inverse_bot_position import InverseBotPosition
from app.models.latency_arb_config import LatencyArbConfig
from app.models.market import Market
from app.models.market_maker_config import MarketMakerConfig
from app.models.notification_channel import NotificationChannel
from app.models.notification_delivery import NotificationDelivery
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.notification_followed_trader import NotificationFollowedTrader
from app.models.redemption_attempt import RedemptionAttempt
from app.models.stop_loss import StopLossOrder
from app.models.take_profit import TakeProfitOrder
from app.models.token import RefreshToken
from app.models.trade_history import TradeHistory
from app.models.trader_position_state import TraderPositionState
from app.models.user import User
from app.models.user_notification_preference import UserNotificationPreference
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.models.whale_config import WhaleConfig
from app.models.whale_event import WhaleEvent
from app.models.winner import Winner

__all__ = [
    "Base",
    "User",
    "UserSettings",
    "Market",
    "Winner",
    "TradeHistory",
    "Assessment",
    "UserTrade",
    "RefreshToken",
    "FollowedTrader",
    "NotificationFollowedTrader",
    "NotificationFeedEvent",
    "NotificationChannel",
    "NotificationDelivery",
    "UserNotificationPreference",
    "TraderPositionState",
    "InverseBotPosition",
    "InverseBotAction",
    "AdminAuditLog",
    "MarketMakerConfig",
    "BacktestRun",
    "TakeProfitOrder",
    "RedemptionAttempt",
    "WhaleEvent",
    "WhaleConfig",
    "StopLossOrder",
    "LatencyArbConfig",
]
