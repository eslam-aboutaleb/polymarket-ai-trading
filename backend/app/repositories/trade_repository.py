"""Compatibility exports for the former flat trading repository module.

New code should import from :mod:`app.repositories.trading` or
:mod:`app.repositories.notifications`. Query implementations live in those
domain packages so the legacy module does not own a second copy of them.
"""

from app.repositories.notifications.feed import NotificationFeedRepository
from app.repositories.notifications.followed_traders import NotificationFollowRepository
from app.repositories.trading.follows import TradingFollowRepository
from app.repositories.trading.orders import (
    StopLossRepository,
    TakeProfitRepository,
    UserTradeRepository,
)

FollowedTraderRepository = TradingFollowRepository
NotificationFollowedTraderRepository = NotificationFollowRepository
NotificationFeedEventRepository = NotificationFeedRepository

__all__ = [
    "FollowedTraderRepository",
    "NotificationFeedEventRepository",
    "NotificationFollowedTraderRepository",
    "StopLossRepository",
    "TakeProfitRepository",
    "UserTradeRepository",
]
