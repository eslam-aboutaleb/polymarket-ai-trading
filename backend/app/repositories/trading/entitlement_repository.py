"""Trading configuration and follower queries used by entitlement policy."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.market_maker_config import MarketMakerConfig


class TradingEntitlementRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def count_followed_traders(self, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(FollowedTrader)
            .where(FollowedTrader.user_id == user_id)
        )
        return int(self.db.execute(stmt).scalar_one())

    def list_enabled_market_makers(self, user_id: int) -> list[MarketMakerConfig]:
        stmt = select(MarketMakerConfig).where(
            MarketMakerConfig.user_id == user_id,
            MarketMakerConfig.enabled.is_(True),
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_latency_arb_config(self, user_id: int) -> LatencyArbConfig | None:
        stmt = select(LatencyArbConfig).where(LatencyArbConfig.user_id == user_id).limit(1)
        return self.db.execute(stmt).scalars().first()
