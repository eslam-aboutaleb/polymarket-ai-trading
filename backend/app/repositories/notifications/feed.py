"""Repository queries for the followed-trader notification feed."""

from __future__ import annotations

from app.models.notifications.notification_feed_event import NotificationFeedEvent
from app.repositories.base import SQLAlchemyRepository


class NotificationFeedRepository(SQLAlchemyRepository[NotificationFeedEvent]):
    model = NotificationFeedEvent

    def list_recent(
        self,
        user_id: int,
        *,
        wallet: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[NotificationFeedEvent]:
        query = self.db.query(NotificationFeedEvent).filter(
            NotificationFeedEvent.user_id == user_id
        )
        if wallet:
            query = query.filter(NotificationFeedEvent.trader_wallet == wallet.lower())
        if event_type:
            query = query.filter(NotificationFeedEvent.event_type == event_type)
        return query.order_by(NotificationFeedEvent.created_at.desc()).limit(limit).all()
