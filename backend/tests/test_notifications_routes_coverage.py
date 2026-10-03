"""Coverage tests for ``app.api.routes.notifications``.

Exercises every route handler (channel CRUD, in-app feed,
preference defaults/upserts, test-alert dispatch) plus the
response-shaping and validation helpers, against a real
in-memory SQLite database with the auth and DB dependencies
overridden and the alert service and encryption layer mocked.
"""

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.api.routes import notifications as notifications_module
from app.api.routes.auth import get_current_user_from_token
from app.main import app
from app.models.base import Base
from app.models.notification_channel import NotificationChannel
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.user import User
from app.models.user_notification_preference import UserNotificationPreference
from app.services.alert_service import ALERT_EVENT_TYPES
from app.utils.database import get_db

USER_ID = 1
OTHER_USER_ID = 2
WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20


class NotificationsRouteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.add(User(id=OTHER_USER_ID, wallet_address=OTHER_WALLET))
        self.db.commit()

        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": USER_ID,
            "wallet_address": WALLET,
            "is_admin": False,
        }

        encrypt_patcher = patch(
            "app.api.routes.notifications.encrypt_text",
            side_effect=lambda value: (value, "key-id-1"),
        )
        encrypt_patcher.start()
        self.addCleanup(encrypt_patcher.stop)

        self.alert_service = MagicMock()
        self.alert_service.send_test_alert = AsyncMock(return_value={"status": "dispatched"})
        alert_patcher = patch(
            "app.api.routes.notifications.get_alert_service",
            return_value=self.alert_service,
        )
        alert_patcher.start()
        self.addCleanup(alert_patcher.stop)

        self.client = TestClient(app)

        self.addCleanup(app.dependency_overrides.clear)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _add_channel(
        self,
        user_id=USER_ID,
        channel_type="telegram",
        name="Channel",
        created_at=None,
        config_json='{"chat_id": "1"}',
        is_active=True,
    ):
        channel = NotificationChannel(
            user_id=user_id,
            channel_type=channel_type,
            name=name,
            config_json=config_json,
            is_active=is_active,
            created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.db.add(channel)
        self.db.commit()
        self.db.refresh(channel)
        return channel

    def _add_event(
        self,
        user_id=USER_ID,
        event_type="fill",
        created_at=None,
        read_at=None,
    ):
        event = NotificationFeedEvent(
            user_id=user_id,
            trader_wallet=WALLET,
            event_type=event_type,
            market_id="0xmarket",
            token_id="0xtoken",
            side="BUY",
            size=1.5,
            price=0.5,
            created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
            read_at=read_at,
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event

    # ── Channel management ──────────────────────────

    def test_list_channels_empty(self):
        response = self.client.get("/api/notifications/channels")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_channels_returns_only_own_channels_ordered(self):
        older = self._add_channel(created_at=datetime(2026, 1, 1, tzinfo=UTC), name="Older")
        newer = self._add_channel(created_at=datetime(2026, 1, 2, tzinfo=UTC), name="Newer")
        self._add_channel(user_id=OTHER_USER_ID, name="Other user's")

        response = self.client.get("/api/notifications/channels")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([c["id"] for c in body], [older.id, newer.id])
        self.assertEqual([c["name"] for c in body], ["Older", "Newer"])
        for channel in body:
            self.assertNotIn("config_json", channel)
            self.assertEqual(channel["is_active"], True)

    def test_create_telegram_channel(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={
                "channel_type": "telegram",
                "name": "My Telegram",
                "chat_id": "  12345  ",
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["channel_type"], "telegram")
        self.assertEqual(body["name"], "My Telegram")
        self.assertTrue(body["is_active"])
        self.assertIsNotNone(body["created_at"])

        row = self.db.query(NotificationChannel).first()
        self.assertEqual(row.user_id, USER_ID)
        self.assertEqual(json.loads(row.config_json), {"chat_id": "12345"})

    def test_create_telegram_requires_chat_id(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={"channel_type": "telegram", "chat_id": "   "},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("chat_id is required", response.json()["detail"])

    def test_create_discord_requires_webhook_url(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={"channel_type": "discord"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("webhook_url is required", response.json()["detail"])

    def test_create_discord_rejects_non_https(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={
                "channel_type": "discord",
                "webhook_url": "http://discord.example/hook",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid https URL", response.json()["detail"])

    def test_create_discord_accepts_https(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={
                "channel_type": "discord",
                "webhook_url": "https://discord.example/hook",
            },
        )
        self.assertEqual(response.status_code, 201)
        row = self.db.query(NotificationChannel).first()
        self.assertEqual(
            json.loads(row.config_json),
            {"webhook_url": "https://discord.example/hook"},
        )

    def test_create_webhook_requires_url(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={"channel_type": "webhook"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("url is required", response.json()["detail"])

    def test_create_webhook_rejects_non_https(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={"channel_type": "webhook", "url": "ftp://example.com/hook"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid https URL", response.json()["detail"])

    def test_create_webhook_with_secret(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={
                "channel_type": "webhook",
                "url": "https://example.com/hook",
                "secret": "  signing-secret  ",
            },
        )
        self.assertEqual(response.status_code, 201)
        row = self.db.query(NotificationChannel).first()
        self.assertEqual(
            json.loads(row.config_json),
            {"url": "https://example.com/hook", "secret": "signing-secret"},
        )

    def test_create_rejects_unknown_channel_type(self):
        response = self.client.post(
            "/api/notifications/channels",
            json={"channel_type": "carrier-pigeon"},
        )
        self.assertEqual(response.status_code, 422)

    def test_delete_channel(self):
        channel = self._add_channel()
        response = self.client.delete(f"/api/notifications/channels/{channel.id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertIsNone(self.db.query(NotificationChannel).filter_by(id=channel.id).first())

    def test_delete_missing_channel_returns_404(self):
        response = self.client.delete("/api/notifications/channels/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Notification channel not found")

    def test_delete_other_users_channel_returns_404(self):
        channel = self._add_channel(user_id=OTHER_USER_ID)
        response = self.client.delete(f"/api/notifications/channels/{channel.id}")
        self.assertEqual(response.status_code, 404)

    # ── In-app alert feed ───────────────────────────

    def test_list_notifications_empty(self):
        response = self.client.get("/api/notifications")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_list_notifications_newest_first(self):
        older = self._add_event(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        newer = self._add_event(created_at=datetime(2026, 1, 2, tzinfo=UTC))
        self._add_event(user_id=OTHER_USER_ID)

        response = self.client.get("/api/notifications")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([e["id"] for e in body], [newer.id, older.id])
        event = body[0]
        self.assertEqual(event["event_type"], "fill")
        self.assertEqual(event["trader_wallet"], WALLET)
        self.assertEqual(event["market_id"], "0xmarket")
        self.assertEqual(event["token_id"], "0xtoken")
        self.assertEqual(event["side"], "BUY")
        self.assertEqual(event["size"], 1.5)
        self.assertEqual(event["price"], 0.5)
        self.assertIsNone(event["read_at"])
        self.assertTrue(event["unread"])
        self.assertIsNotNone(event["created_at"])

    def test_list_notifications_unread_only(self):
        self._add_event(read_at=datetime(2026, 1, 2, tzinfo=UTC))
        unread = self._add_event(created_at=datetime(2026, 1, 3, tzinfo=UTC))

        response = self.client.get("/api/notifications?unread_only=true")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([e["id"] for e in body], [unread.id])
        self.assertTrue(body[0]["unread"])

    def test_list_notifications_pagination(self):
        for i in range(3):
            self._add_event(created_at=datetime(2026, 1, 1 + i, tzinfo=UTC))

        first_page = self.client.get("/api/notifications?limit=2")
        self.assertEqual(len(first_page.json()), 2)

        second_page = self.client.get("/api/notifications?skip=2&limit=2")
        self.assertEqual(len(second_page.json()), 1)

    def test_list_notifications_limit_above_max(self):
        response = self.client.get("/api/notifications?limit=201")
        self.assertEqual(response.status_code, 422)

    def test_mark_notification_read(self):
        event = self._add_event()
        response = self.client.patch(f"/api/notifications/{event.id}/read")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNotNone(body["read_at"])
        self.assertFalse(body["unread"])
        self.db.refresh(event)
        self.assertIsNotNone(event.read_at)

    def test_mark_notification_read_missing_returns_404(self):
        response = self.client.patch("/api/notifications/999/read")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Notification not found")

    def test_mark_other_users_notification_returns_404(self):
        event = self._add_event(user_id=OTHER_USER_ID)
        response = self.client.patch(f"/api/notifications/{event.id}/read")
        self.assertEqual(response.status_code, 404)

    # ── Preferences ─────────────────────────────────

    def test_get_preferences_returns_defaults_for_every_event_type(self):
        response = self.client.get("/api/notifications/preferences")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(sorted(p["event_type"] for p in body), sorted(ALERT_EVENT_TYPES))
        for preference in body:
            self.assertEqual(preference["channel_ids"], [])
            self.assertTrue(preference["enabled"])
            self.assertTrue(preference["is_default"])

    def test_get_preferences_includes_stored_rows(self):
        channel = self._add_channel()
        self.db.add(
            UserNotificationPreference(
                user_id=USER_ID,
                event_type="fill",
                channel_ids=json.dumps([channel.id]),
                enabled=False,
            )
        )
        self.db.commit()

        response = self.client.get("/api/notifications/preferences")

        self.assertEqual(response.status_code, 200)
        fill = next(p for p in response.json() if p["event_type"] == "fill")
        self.assertEqual(fill["channel_ids"], [channel.id])
        self.assertFalse(fill["enabled"])
        self.assertFalse(fill["is_default"])

    def test_get_preferences_tolerates_corrupted_channel_ids(self):
        self.db.add(
            UserNotificationPreference(
                user_id=USER_ID, event_type="fill", channel_ids="not-json", enabled=True
            )
        )
        self.db.commit()

        response = self.client.get("/api/notifications/preferences")

        fill = next(p for p in response.json() if p["event_type"] == "fill")
        self.assertEqual(fill["channel_ids"], [])

    def test_update_preferences_inserts_new_rows(self):
        channel = self._add_channel()
        response = self.client.put(
            "/api/notifications/preferences",
            json={
                "preferences": [
                    {"event_type": "fill", "channel_ids": [channel.id], "enabled": True},
                    {"event_type": "whale_alert", "channel_ids": [], "enabled": False},
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        fill = next(p for p in body if p["event_type"] == "fill")
        self.assertEqual(fill["channel_ids"], [channel.id])
        self.assertTrue(fill["enabled"])
        whale = next(p for p in body if p["event_type"] == "whale_alert")
        self.assertEqual(whale["channel_ids"], [])
        self.assertFalse(whale["enabled"])

        rows = self.db.query(UserNotificationPreference).filter_by(user_id=USER_ID).all()
        self.assertEqual(len(rows), 2)

    def test_update_preferences_updates_existing_rows(self):
        channel = self._add_channel()
        other_channel = self._add_channel(name="Second")
        self.db.add(
            UserNotificationPreference(
                user_id=USER_ID,
                event_type="fill",
                channel_ids=json.dumps([channel.id]),
                enabled=True,
            )
        )
        self.db.commit()

        response = self.client.put(
            "/api/notifications/preferences",
            json={
                "preferences": [
                    {
                        "event_type": "fill",
                        "channel_ids": [other_channel.id],
                        "enabled": False,
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 200)

        row = (
            self.db.query(UserNotificationPreference)
            .filter_by(user_id=USER_ID, event_type="fill")
            .first()
        )
        self.assertEqual(row.channel_ids, json.dumps([other_channel.id]))
        self.assertFalse(row.enabled)
        self.assertIsNotNone(row.updated_at)

    def test_update_preferences_rejects_unknown_event_type(self):
        response = self.client.put(
            "/api/notifications/preferences",
            json={"preferences": [{"event_type": "not-an-event", "channel_ids": []}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown event type: not-an-event", response.json()["detail"])

    def test_update_preferences_rejects_unowned_channel(self):
        self._add_channel(user_id=OTHER_USER_ID)
        response = self.client.put(
            "/api/notifications/preferences",
            json={"preferences": [{"event_type": "fill", "channel_ids": [999]}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Channel ids not owned by user", response.json()["detail"])

    def test_update_preferences_with_empty_batch(self):
        response = self.client.put("/api/notifications/preferences", json={"preferences": []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), len(ALERT_EVENT_TYPES))

    # ── Test alert ──────────────────────────────────

    def test_send_test_alert_to_specific_channel(self):
        channel = self._add_channel()
        response = self.client.post(
            "/api/notifications/test",
            json={"channel_id": channel.id, "message": "Hello"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "dispatched"})
        self.alert_service.send_test_alert.assert_awaited_once_with(
            USER_ID, channel_id=channel.id, db=self.db, message="Hello"
        )

    def test_send_test_alert_to_all_channels(self):
        response = self.client.post("/api/notifications/test", json={})
        self.assertEqual(response.status_code, 200)
        self.alert_service.send_test_alert.assert_awaited_once_with(
            USER_ID,
            channel_id=None,
            db=self.db,
            message="Test alert from Polymarket",
        )

    def test_send_test_alert_to_unowned_channel_returns_404(self):
        self._add_channel(user_id=OTHER_USER_ID)
        response = self.client.post("/api/notifications/test", json={"channel_id": 999})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Notification channel not found")
        self.alert_service.send_test_alert.assert_not_awaited()


class ResponseHelperTests(unittest.TestCase):
    def test_channel_response_defaults(self):
        channel = NotificationChannel(
            user_id=USER_ID, channel_type="telegram", name=None, config_json="{}"
        )
        channel.id = 1
        response = notifications_module._channel_response(channel)
        self.assertEqual(response.name, "")
        self.assertIsNone(response.created_at)
        self.assertFalse(response.is_active)

    def test_event_response_defaults(self):
        event = NotificationFeedEvent(
            user_id=USER_ID,
            trader_wallet=None,
            event_type="fill",
            market_id=None,
            token_id=None,
            side=None,
            size=None,
            price=None,
        )
        event.id = 1
        response = notifications_module._event_response(event)
        self.assertEqual(response.event_type, "fill")
        self.assertEqual(response.trader_wallet, "")
        self.assertEqual(response.market_id, "")
        self.assertEqual(response.token_id, "")
        self.assertEqual(response.side, "")
        self.assertEqual(response.size, 0.0)
        self.assertEqual(response.price, 0.0)
        self.assertEqual(response.created_at, "")
        self.assertIsNone(response.read_at)
        self.assertTrue(response.unread)

    def test_event_response_with_read_at(self):
        event = NotificationFeedEvent(
            user_id=USER_ID,
            trader_wallet=WALLET,
            event_type="fill",
            read_at=datetime(2026, 1, 2, tzinfo=UTC),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        event.id = 1
        response = notifications_module._event_response(event)
        self.assertFalse(response.unread)
        self.assertEqual(response.read_at, "2026-01-02T00:00:00+00:00")

    def test_preference_response_parses_channel_ids(self):
        preference = UserNotificationPreference(
            user_id=USER_ID, event_type="fill", channel_ids="[1, 2]", enabled=True
        )
        response = notifications_module._preference_response(preference)
        self.assertEqual(response.channel_ids, [1, 2])
        self.assertTrue(response.enabled)

    def test_preference_response_tolerates_invalid_json(self):
        for raw in ("not-json", None, "[1, 'a']"):
            preference = UserNotificationPreference(
                user_id=USER_ID, event_type="fill", channel_ids=raw, enabled=True
            )
            response = notifications_module._preference_response(preference)
            self.assertEqual(response.channel_ids, [])

    def test_require_https_accepts_https_url(self):
        notifications_module._require_https("https://example.com/webhook")

    def test_require_https_rejects_non_https_scheme(self):
        with self.assertRaises(HTTPException) as ctx:
            notifications_module._require_https("http://example.com/webhook")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_require_https_rejects_missing_hostname(self):
        with self.assertRaises(HTTPException) as ctx:
            notifications_module._require_https("https://")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_build_channel_config_telegram_strips_chat_id(self):
        request = notifications_module.ChannelCreateRequest(
            channel_type="telegram", chat_id="  42  "
        )
        self.assertEqual(notifications_module._build_channel_config(request), {"chat_id": "42"})

    def test_build_channel_config_discord_strips_url(self):
        request = notifications_module.ChannelCreateRequest(
            channel_type="discord", webhook_url="  https://discord.com/h  "
        )
        self.assertEqual(
            notifications_module._build_channel_config(request),
            {"webhook_url": "https://discord.com/h"},
        )

    def test_build_channel_config_webhook_includes_secret(self):
        request = notifications_module.ChannelCreateRequest(
            channel_type="webhook",
            url="https://example.com/hook",
            secret="s",
        )
        self.assertEqual(
            notifications_module._build_channel_config(request),
            {"url": "https://example.com/hook", "secret": "s"},
        )


if __name__ == "__main__":
    unittest.main()
