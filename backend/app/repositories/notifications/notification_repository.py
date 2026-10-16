"""Notification channel, preference, feed and delivery queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notifications.notification_channel import NotificationChannel
from app.models.notifications.notification_delivery import NotificationDelivery
from app.models.notifications.notification_feed_event import NotificationFeedEvent
from app.models.notifications.user_notification_preference import UserNotificationPreference


class NotificationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def has_digest_marker(self, user_id: int, event_type: str, day_start: datetime) -> bool:
        stmt = (
            select(NotificationFeedEvent.id)
            .where(
                NotificationFeedEvent.user_id == user_id,
                NotificationFeedEvent.event_type == event_type,
                NotificationFeedEvent.created_at >= day_start,
            )
            .limit(1)
        )
        return self.db.execute(stmt).first() is not None

    def has_active_channels(self, user_id: int) -> bool:
        stmt = (
            select(NotificationChannel.id)
            .where(
                NotificationChannel.user_id == user_id,
                NotificationChannel.is_active.is_(True),
            )
            .limit(1)
        )
        return self.db.execute(stmt).first() is not None

    def get_preference(self, user_id: int, event_type: str) -> UserNotificationPreference | None:
        stmt = (
            select(UserNotificationPreference)
            .where(
                UserNotificationPreference.user_id == user_id,
                UserNotificationPreference.event_type == event_type,
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def list_active_channel_ids(self, user_id: int) -> list[int]:
        stmt = select(NotificationChannel.id).where(
            NotificationChannel.user_id == user_id,
            NotificationChannel.is_active.is_(True),
        )
        return list(self.db.execute(stmt).scalars().all())

    def list_channel_ids(self, user_id: int) -> list[int]:
        stmt = select(NotificationChannel.id).where(NotificationChannel.user_id == user_id)
        return list(self.db.execute(stmt).scalars().all())

    def list_channels_for_user(
        self, user_id: int, channel_ids: list[int]
    ) -> list[NotificationChannel]:
        stmt = select(NotificationChannel).where(
            NotificationChannel.id.in_(channel_ids),
            NotificationChannel.user_id == user_id,
        )
        return list(self.db.execute(stmt).scalars().all())

    def list_channels(self, user_id: int) -> list[NotificationChannel]:
        stmt = (
            select(NotificationChannel)
            .where(NotificationChannel.user_id == user_id)
            .order_by(NotificationChannel.created_at.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_owned_channel(self, user_id: int, channel_id: int) -> NotificationChannel | None:
        stmt = (
            select(NotificationChannel)
            .where(
                NotificationChannel.id == channel_id,
                NotificationChannel.user_id == user_id,
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def create_channel(self, channel: NotificationChannel) -> NotificationChannel:
        self.db.add(channel)
        self.db.commit()
        self.db.refresh(channel)
        return channel

    def delete_owned_channel(self, user_id: int, channel_id: int) -> bool:
        channel = self.get_owned_channel(user_id, channel_id)
        if channel is None:
            return False
        self.db.delete(channel)
        self.db.commit()
        return True

    def list_feed(
        self, user_id: int, *, unread_only: bool, offset: int, limit: int
    ) -> list[NotificationFeedEvent]:
        stmt = select(NotificationFeedEvent).where(NotificationFeedEvent.user_id == user_id)
        if unread_only:
            stmt = stmt.where(NotificationFeedEvent.read_at.is_(None))
        stmt = stmt.order_by(NotificationFeedEvent.created_at.desc()).offset(offset).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def get_owned_feed_event(self, user_id: int, event_id: int) -> NotificationFeedEvent | None:
        stmt = (
            select(NotificationFeedEvent)
            .where(
                NotificationFeedEvent.id == event_id,
                NotificationFeedEvent.user_id == user_id,
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def mark_feed_event_read(
        self, user_id: int, event_id: int, read_at: datetime
    ) -> NotificationFeedEvent | None:
        event = self.get_owned_feed_event(user_id, event_id)
        if event is None:
            return None
        event.read_at = read_at
        self.db.commit()
        self.db.refresh(event)
        return event

    def list_preferences(self, user_id: int) -> list[UserNotificationPreference]:
        stmt = select(UserNotificationPreference).where(
            UserNotificationPreference.user_id == user_id
        )
        return list(self.db.execute(stmt).scalars().all())

    def save_preferences(
        self, user_id: int, preferences: list[tuple[str, list[int], bool]], updated_at: datetime
    ) -> list[UserNotificationPreference]:
        import json

        for event_type, channel_ids, enabled in preferences:
            row = self.get_preference(user_id, event_type)
            if row is None:
                row = UserNotificationPreference(
                    user_id=user_id,
                    event_type=event_type,
                    channel_ids=json.dumps(channel_ids),
                    enabled=enabled,
                )
                self.db.add(row)
            else:
                row.channel_ids = json.dumps(channel_ids)
                row.enabled = enabled
                row.updated_at = updated_at
        self.db.commit()
        return self.list_preferences(user_id)

    def get_feed_event(self, event_id: Any) -> NotificationFeedEvent | None:
        return self.db.get(NotificationFeedEvent, event_id)

    def get_delivery(self, delivery_id: int) -> NotificationDelivery | None:
        return self.db.get(NotificationDelivery, delivery_id)

    def list_feed_events_since(
        self, user_id: int, since: datetime, limit: int
    ) -> list[NotificationFeedEvent]:
        stmt = (
            select(NotificationFeedEvent)
            .where(
                NotificationFeedEvent.user_id == user_id,
                NotificationFeedEvent.created_at >= since,
            )
            .order_by(NotificationFeedEvent.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())
