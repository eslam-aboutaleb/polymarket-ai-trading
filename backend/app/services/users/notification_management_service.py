"""Notification-management reads used by the HTTP routes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.notifications.notification_channel import NotificationChannel
from app.models.notifications.notification_feed_event import NotificationFeedEvent
from app.models.notifications.user_notification_preference import UserNotificationPreference
from app.repositories.notifications.notification_repository import NotificationRepository
from app.utils.time import utc_now


def list_channels(db: Session, user_id: int) -> list[NotificationChannel]:
    return NotificationRepository(db).list_channels(user_id)


def get_owned_channel(db: Session, user_id: int, channel_id: int) -> NotificationChannel | None:
    return NotificationRepository(db).get_owned_channel(user_id, channel_id)


def create_channel(
    db: Session, *, user_id: int, channel_type: str, name: str, encrypted_config: str
) -> NotificationChannel:
    channel = NotificationChannel(
        user_id=user_id,
        channel_type=channel_type,
        name=name,
        config_json=encrypted_config,
    )
    return NotificationRepository(db).create_channel(channel)


def delete_owned_channel(db: Session, user_id: int, channel_id: int) -> bool:
    return NotificationRepository(db).delete_owned_channel(user_id, channel_id)


def list_feed(
    db: Session, user_id: int, *, unread_only: bool = False, offset: int = 0, limit: int = 50
) -> list[NotificationFeedEvent]:
    return NotificationRepository(db).list_feed(
        user_id, unread_only=unread_only, offset=offset, limit=limit
    )


def get_owned_feed_event(db: Session, user_id: int, event_id: int) -> NotificationFeedEvent | None:
    return NotificationRepository(db).get_owned_feed_event(user_id, event_id)


def mark_feed_event_read(db: Session, user_id: int, event_id: int) -> NotificationFeedEvent | None:
    return NotificationRepository(db).mark_feed_event_read(user_id, event_id, utc_now())


def list_preferences(db: Session, user_id: int) -> list[UserNotificationPreference]:
    return NotificationRepository(db).list_preferences(user_id)


def list_channel_ids(db: Session, user_id: int) -> list[int]:
    return NotificationRepository(db).list_channel_ids(user_id)


def get_preference(db: Session, user_id: int, event_type: str) -> UserNotificationPreference | None:
    return NotificationRepository(db).get_preference(user_id, event_type)


def update_preferences(
    db: Session, user_id: int, preferences: list[tuple[str, list[int], bool]]
) -> list[UserNotificationPreference]:
    return NotificationRepository(db).save_preferences(user_id, preferences, utc_now())
