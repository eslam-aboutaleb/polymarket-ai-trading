"""SQLAlchemy data-access layer and compatibility repository exports.

Application persistence flows follow
``app.api.routes`` -> ``app.services.<domain>`` -> ``app.repositories.<domain>``
-> ``app.models.<domain>``. Domain modules own query construction; this root
package keeps common historical imports working while callers migrate to the
canonical modules. See ``docs/backend/ARCHITECTURE.md`` for the layer boundaries.
"""

from app.repositories.base import SQLAlchemyRepository
from app.repositories.billing.subscription_repository import SubscriptionRepository
from app.repositories.market_data.markets import MarketRepository
from app.repositories.market_data.venues import VenueMarketRepository
from app.repositories.notifications.feed import NotificationFeedRepository
from app.repositories.notifications.followed_traders import NotificationFollowRepository
from app.repositories.trading.follows import TradingFollowRepository
from app.repositories.trading.orders import (
    StopLossRepository,
    TakeProfitRepository,
    UserTradeRepository,
)
from app.repositories.users.account_repository import AccountRepository
from app.repositories.users.api_key_repository import ApiKeyRepository
from app.repositories.users.settings import UserSettingsRepository
from app.repositories.users.user_repository import UserCreationError, UserRepository

__all__ = [
    "AccountRepository",
    "ApiKeyRepository",
    "FollowedTraderRepository",
    "MarketRepository",
    "NotificationFeedEventRepository",
    "NotificationFollowedTraderRepository",
    "SQLAlchemyRepository",
    "StopLossRepository",
    "SubscriptionRepository",
    "TakeProfitRepository",
    "UserCreationError",
    "UserRepository",
    "UserSettingsRepository",
    "UserTradeRepository",
    "VenueMarketRepository",
]

# Compatibility names retained for callers that still import the original
# flat repository API. New code should import from the domain packages.
FollowedTraderRepository = TradingFollowRepository
NotificationFeedEventRepository = NotificationFeedRepository
NotificationFollowedTraderRepository = NotificationFollowRepository
