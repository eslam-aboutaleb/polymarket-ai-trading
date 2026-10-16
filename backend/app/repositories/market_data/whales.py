"""Whale event, follow and configuration queries."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.market_data.whale_config import WhaleConfig
from app.models.market_data.whale_event import WhaleEvent
from app.models.trading.followed_trader import FollowedTrader


class WhaleRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def active_follow_wallets(self):
        return (
            self.db.query(FollowedTrader.user_id, FollowedTrader.trader_wallet)
            .filter(FollowedTrader.is_active.is_(True))
            .all()
        )

    def watchlists(self):
        return self.db.query(WhaleConfig.user_id, WhaleConfig.watchlist).all()

    def get_config(self, user_id: int) -> WhaleConfig | None:
        return self.db.query(WhaleConfig).filter(WhaleConfig.user_id == user_id).first()

    def get_event(self, event_id: int) -> WhaleEvent | None:
        return self.db.get(WhaleEvent, event_id)

    def list_since(self, cutoff: datetime, limit: int) -> list[WhaleEvent]:
        return (
            self.db.query(WhaleEvent)
            .filter(WhaleEvent.detected_at >= cutoff)
            .order_by(WhaleEvent.detected_at.desc())
            .limit(limit)
            .all()
        )

    def list_for_markets_since(
        self, market_ids: list[str], since: datetime, limit: int
    ) -> list[WhaleEvent]:
        return (
            self.db.query(WhaleEvent)
            .filter(WhaleEvent.market_id.in_(market_ids), WhaleEvent.detected_at >= since)
            .order_by(WhaleEvent.detected_at.desc())
            .limit(limit)
            .all()
        )

    def list_filtered(
        self,
        *,
        wallet: str | None,
        market_id: str | None,
        side: str | None,
        event_type: str | None,
        offset: int,
        limit: int,
    ) -> tuple[int, list[WhaleEvent]]:
        query = self.db.query(WhaleEvent)
        if wallet:
            query = query.filter(WhaleEvent.wallet == wallet)
        if market_id:
            query = query.filter(WhaleEvent.market_id == market_id)
        if side:
            query = query.filter(WhaleEvent.side == side)
        if event_type:
            query = query.filter(WhaleEvent.event_type == event_type)
        total = query.count()
        rows = query.order_by(WhaleEvent.detected_at.desc()).offset(offset).limit(limit).all()
        return total, rows
