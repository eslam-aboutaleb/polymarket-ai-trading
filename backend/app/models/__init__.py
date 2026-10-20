"""SQLAlchemy model registry for Alembic and application startup.

Importing this package registers every mapped entity with ``Base.metadata``.
Model definitions live under their bounded-context packages.
"""

from app.models.analytics.assessment import Assessment
from app.models.analytics.backtest_run import BacktestRun
from app.models.analytics.portfolio_snapshot import PortfolioSnapshot
from app.models.base import Base
from app.models.billing.subscription import BillingWebhookEvent, Subscription
from app.models.intelligence.collaboration import ForecastVote, MarketAnnotation, UserFollow
from app.models.intelligence.governance import LLMExperiment, VerdictFeedback
from app.models.market_data.market import Market
from app.models.market_data.venue_market import VenueMarket
from app.models.market_data.whale_config import WhaleConfig
from app.models.market_data.whale_event import WhaleEvent
from app.models.market_data.winner import Winner
from app.models.notifications.notification_channel import NotificationChannel
from app.models.notifications.notification_delivery import NotificationDelivery
from app.models.notifications.notification_feed_event import NotificationFeedEvent
from app.models.notifications.notification_followed_trader import NotificationFollowedTrader
from app.models.notifications.user_notification_preference import UserNotificationPreference
from app.models.platform.app_event import AppEvent
from app.models.platform.audit_log import AdminAuditLog
from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.inverse_bot_action import InverseBotAction
from app.models.trading.inverse_bot_position import InverseBotPosition
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.market_maker_config import MarketMakerConfig
from app.models.trading.redemption_attempt import RedemptionAttempt
from app.models.trading.stop_loss import StopLossOrder
from app.models.trading.take_profit import TakeProfitOrder
from app.models.trading.trade_history import TradeHistory
from app.models.trading.trader_position_state import TraderPositionState
from app.models.trading.trading_control import EmergencyHaltOperation, TradingControlState
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account
from app.models.users.account_settings import AccountSettings
from app.models.users.api_key import ApiKey
from app.models.users.token import RefreshToken
from app.models.users.user import User
from app.models.users.user_settings import UserSettings

__all__ = [
    "Base",
    "TradingControlState",
    "EmergencyHaltOperation",
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
    "PortfolioSnapshot",
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
    "AppEvent",
    "VerdictFeedback",
    "LLMExperiment",
    "Account",
    "AccountSettings",
    "ApiKey",
    "VenueMarket",
    "ForecastVote",
    "MarketAnnotation",
    "UserFollow",
    "Subscription",
    "BillingWebhookEvent",
]
