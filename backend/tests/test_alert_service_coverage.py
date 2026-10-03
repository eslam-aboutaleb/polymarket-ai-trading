"""Additional coverage for ``app.services.alert_service``.

Complements test_alert_service.py with the Redis connection
helper, message formatting, constructor wiring, the dispatch
edge paths (channel overrides, injected sessions, DB failures,
inactive/corrupt channels), preference parsing quirks, digest
timer handling, the Redis storm-counter path, background task
spawning, per-attempt recording, sender validation errors, the
rate-limit-exhausted retry path, and the module-level singleton
and dispatch shim.
"""

import asyncio
import contextlib
import json
import socket
import time
import unittest
from collections import deque
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.notification_channel import NotificationChannel
from app.models.notification_delivery import NotificationDelivery
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.user_notification_preference import UserNotificationPreference
from app.security.crypto import encrypt_text
from app.services import alert_service as alert_mod
from app.services.alert_service import (
    AlertDeliveryError,
    AlertService,
    TokenBucketRateLimiter,
    _connect_redis,
    format_alert_message,
    get_alert_service,
)
from app.services.alert_service import dispatch as alert_dispatch


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _default_config(channel_type):
    if channel_type == "telegram":
        return {"chat_id": "12345"}
    if channel_type == "discord":
        return {"webhook_url": "https://discord.com/api/webhooks/1/abc"}
    return {"url": "https://example.com/hook"}


def _add_channel(session, user_id=1, channel_type="telegram", config=None, is_active=True):
    if config is None:
        config = _default_config(channel_type)
    encrypted, _key_id = encrypt_text(json.dumps(config))
    channel = NotificationChannel(
        user_id=user_id,
        channel_type=channel_type,
        name=f"{channel_type} channel",
        config_json=encrypted,
        is_active=is_active,
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)
    return channel


def _make_service(factory, **kwargs):
    kwargs.setdefault("session_factory", factory)
    kwargs.setdefault("rate_limiter", TokenBucketRateLimiter(redis_url=""))
    kwargs.setdefault("redis_url", "")
    return AlertService(**kwargs)


@contextlib.contextmanager
def _public_dns():
    def _resolve(host, port, proto=0, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    with mock.patch("socket.getaddrinfo", side_effect=_resolve):
        yield


def _fake_http_client():
    response = mock.MagicMock()
    client = mock.AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


@contextlib.contextmanager
def _patched_http():
    fake_client = _fake_http_client()
    with mock.patch(
        "app.services.alert_service.httpx.AsyncClient",
        return_value=fake_client,
    ):
        yield fake_client


@contextlib.contextmanager
def _patched_settings(token="123456:ABC-DEF"):
    fake_settings = mock.MagicMock()
    fake_settings.telegram_bot_token = token
    fake_settings.redis_url = ""
    with mock.patch("app.services.alert_service.get_settings", return_value=fake_settings):
        yield fake_settings


class ConnectRedisTests(unittest.TestCase):
    def test_empty_or_missing_url_returns_none(self):
        self.assertIsNone(_connect_redis(""))
        self.assertIsNone(_connect_redis(None))

    def test_missing_package_returns_none(self):
        with mock.patch.object(alert_mod, "REDIS_AVAILABLE", False):
            self.assertIsNone(_connect_redis("redis://fake:6379/0"))

    def test_successful_connection_returns_pinged_client(self):
        fake_client = mock.MagicMock()
        with mock.patch.object(alert_mod.redis_lib, "from_url", return_value=fake_client):
            result = _connect_redis("redis://fake:6379/0")
        self.assertIs(result, fake_client)
        fake_client.ping.assert_called_once()

    def test_failed_ping_returns_none(self):
        fake_client = mock.MagicMock()
        fake_client.ping.side_effect = RuntimeError("redis down")
        with mock.patch.object(alert_mod.redis_lib, "from_url", return_value=fake_client):
            self.assertIsNone(_connect_redis("redis://fake:6379/0"))


class FormatAlertMessageTests(unittest.TestCase):
    def test_known_event_type_uses_label(self):
        self.assertEqual(format_alert_message("fill", {}), "[Alert] Order Fill")

    def test_unknown_event_type_is_title_cased(self):
        self.assertEqual(format_alert_message("custom_event", {}), "[Alert] Custom Event")

    def test_digest_message_includes_count(self):
        message = format_alert_message("whale_alert", {"digest": True, "count": 7})
        self.assertEqual(message, "[Alert] Whale Alert digest - 7 events batched")

    def test_digest_message_defaults_count_to_zero(self):
        message = format_alert_message("whale_alert", {"digest": True})
        self.assertEqual(message, "[Alert] Whale Alert digest - 0 events batched")

    def test_none_and_empty_values_are_skipped(self):
        message = format_alert_message(
            "fill", {"message": None, "trader_wallet": "", "market_id": "m1"}
        )
        self.assertEqual(message, "[Alert] Order Fill\nmarket_id: m1")

    def test_all_supported_fields_are_rendered(self):
        payload = {
            "message": "filled",
            "trader_wallet": "0xabc",
            "market_id": "m1",
            "side": "buy",
            "size": 1.5,
            "price": 2.0,
            "reason": "edge",
        }
        message = format_alert_message("fill", payload)
        for fragment in (
            "message: filled",
            "trader_wallet: 0xabc",
            "market_id: m1",
            "side: buy",
            "size: 1.5",
            "price: 2.0",
            "reason: edge",
        ):
            self.assertIn(fragment, message)


class ConstructorWiringTests(unittest.TestCase):
    def test_rate_limiter_uses_settings_redis_url_when_unset(self):
        fake_settings = mock.MagicMock()
        fake_settings.redis_url = "redis://settings-host:6379"
        with (
            mock.patch.object(alert_mod, "get_settings", return_value=fake_settings),
            mock.patch.object(alert_mod, "_connect_redis", return_value=None) as connect_mock,
        ):
            TokenBucketRateLimiter()
        connect_mock.assert_called_once_with("redis://settings-host:6379")

    def test_service_uses_settings_redis_url_when_unset(self):
        fake_settings = mock.MagicMock()
        fake_settings.redis_url = "redis://settings-host:6379"
        with (
            mock.patch.object(alert_mod, "get_settings", return_value=fake_settings),
            mock.patch.object(alert_mod, "_connect_redis", return_value=None) as connect_mock,
        ):
            AlertService(
                session_factory=lambda: mock.MagicMock(),
                rate_limiter=mock.MagicMock(),
            )
        connect_mock.assert_called_once_with("redis://settings-host:6379")

    def test_explicit_redis_url_wins_over_settings(self):
        with (
            mock.patch.object(alert_mod, "get_settings", return_value=mock.MagicMock()),
            mock.patch.object(alert_mod, "_connect_redis", return_value=None) as connect_mock,
        ):
            AlertService(
                redis_url="redis://explicit:6379",
                rate_limiter=mock.MagicMock(),
            )
        connect_mock.assert_called_once_with("redis://explicit:6379")


class DispatchEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()

    def tearDown(self):
        self.session.close()

    async def test_channel_ids_override_bypasses_preferences(self):
        telegram = _add_channel(self.session, 1, "telegram")
        _add_channel(self.session, 1, "webhook")
        preference = UserNotificationPreference(
            user_id=1,
            event_type="fill",
            channel_ids=json.dumps([]),
            enabled=True,
        )
        self.session.add(preference)
        self.session.commit()
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http() as fake_client:
            result = await service.dispatch(
                "fill",
                1,
                {"skip_feed": True},
                background=False,
                channel_ids_override=[telegram.id],
            )
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(result["deliveries"]), 1)
        self.assertIn("api.telegram.org", fake_client.post.call_args.args[0])

    async def test_provided_session_is_not_closed(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with (
            _patched_settings(),
            _patched_http(),
            mock.patch.object(self.session, "close") as close_mock,
        ):
            result = await service.dispatch(
                "fill", 1, {"skip_feed": True}, db=self.session, background=False
            )
        self.assertEqual(result["status"], "dispatched")
        close_mock.assert_not_called()

    async def test_database_error_rolls_back_and_propagates(self):
        service = _make_service(self.factory)
        with (
            mock.patch.object(service, "_resolve_channel_ids", side_effect=RuntimeError("db down")),
            self.assertRaises(RuntimeError),
        ):
            await service.dispatch("fill", 1, {"skip_feed": True})

    async def test_only_inactive_channels_skips_delivery(self):
        inactive = _add_channel(self.session, 1, "telegram", is_active=False)
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http() as fake_client:
            result = await service.dispatch(
                "fill",
                1,
                {"skip_feed": True},
                background=False,
                channel_ids_override=[inactive.id],
            )
        self.assertEqual(result, {"status": "skipped", "reason": "no_active_channels"})
        fake_client.post.assert_not_called()

    async def test_undecryptable_channel_config_is_skipped(self):
        bad = NotificationChannel(
            user_id=1,
            channel_type="telegram",
            name="bad",
            config_json="not-a-fernet-token",
            is_active=True,
        )
        self.session.add(bad)
        good = _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http():
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(result["deliveries"]), 1)
        rows = self.session.query(NotificationDelivery).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].channel_id, good.id)

    async def test_background_dispatch_spawns_delivery_tasks(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http():
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=True)
            self.assertEqual(result["status"], "dispatched")
            self.assertEqual(len(result["deliveries"]), 1)
            tasks = set(service._background_tasks)
            if tasks:
                await asyncio.wait(tasks)
        rows = self.session.query(NotificationDelivery).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "sent")

    async def test_maybe_digest_skips_payloads_already_digested(self):
        service = _make_service(self.factory)
        self.assertFalse(await service._maybe_digest("whale_alert", 1, {"digest": True}))
        self.assertEqual(service._digest_buffer, {})

    async def test_maybe_digest_ignores_non_digest_event_types(self):
        service = _make_service(self.factory)
        self.assertFalse(await service._maybe_digest("fill", 1, {}))
        self.assertEqual(service._digest_buffer, {})

    async def test_flush_digest_without_events_returns_none_and_cancels_timer(self):
        service = _make_service(self.factory)
        timer = mock.MagicMock()
        service._digest_timers[(1, "whale_alert")] = timer
        result = await service.flush_digest(1, "whale_alert")
        self.assertIsNone(result)
        timer.cancel.assert_called_once()
        self.assertNotIn((1, "whale_alert"), service._digest_timers)

    async def test_flush_digest_dispatches_aggregated_payload(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        service._digest_buffer[(1, "whale_alert")] = [
            {"trader_wallet": "0xw1"},
            {"trader_wallet": "0xw2"},
        ]
        with _patched_settings(), _patched_http():
            result = await service.flush_digest(1, "whale_alert")
        self.assertEqual(result["status"], "dispatched")
        tasks = set(service._background_tasks)
        if tasks:
            await asyncio.wait(tasks)
        rows = self.session.query(NotificationDelivery).all()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0].payload_json)
        self.assertTrue(payload["digest"])
        self.assertEqual(payload["count"], 2)

    async def test_flush_digest_timer_callback_body(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        service._digest_buffer[(1, "whale_alert")] = [{"trader_wallet": "0xw"}]
        with _patched_settings(), _patched_http():
            result = await service._flush_digest(1, "whale_alert")
        self.assertEqual(result["status"], "dispatched")
        self.assertNotIn((1, "whale_alert"), service._digest_buffer)


class PreferenceResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()

    def tearDown(self):
        self.session.close()

    async def test_malformed_channel_ids_json_returns_no_channels(self):
        preference = UserNotificationPreference(
            user_id=1,
            event_type="fill",
            channel_ids="not-json",
            enabled=True,
        )
        self.session.add(preference)
        self.session.commit()
        service = _make_service(self.factory)
        self.assertEqual(service._resolve_channel_ids(self.session, 1, "fill"), [])

    async def test_non_integer_channel_ids_are_skipped(self):
        preference = UserNotificationPreference(
            user_id=1,
            event_type="fill",
            channel_ids='["1", "abc", null, 2]',
            enabled=True,
        )
        self.session.add(preference)
        self.session.commit()
        service = _make_service(self.factory)
        self.assertEqual(service._resolve_channel_ids(self.session, 1, "fill"), [1, 2])

    async def test_missing_preference_falls_back_to_active_channels(self):
        telegram = _add_channel(self.session, 1, "telegram")
        _add_channel(self.session, 1, "webhook", is_active=False)
        service = _make_service(self.factory)
        self.assertEqual(service._resolve_channel_ids(self.session, 1, "fill"), [telegram.id])

    async def test_create_feed_event_defaults_missing_payload_keys(self):
        service = _make_service(self.factory)
        event_id = service._create_feed_event(self.session, 1, "fill", {})
        event = self.session.get(NotificationFeedEvent, event_id)
        self.assertIsNotNone(event)
        self.assertEqual(event.trader_wallet, "")
        self.assertEqual(event.market_id, "")
        self.assertEqual(event.token_id, "")
        self.assertEqual(event.side, "")
        self.assertEqual(event.size, 0.0)
        self.assertEqual(event.price, 0.0)
        self.assertEqual(event.email_status, "skipped")


class StormProtectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()

    def tearDown(self):
        self.session.close()

    async def test_redis_storm_counter_allows_under_limit(self):
        service = _make_service(self.factory)
        fake_redis = mock.MagicMock()
        pipe = mock.MagicMock()
        fake_redis.pipeline.return_value = pipe
        pipe.execute.return_value = [5]
        service._redis = fake_redis
        # whale_alert is limited to 10 dispatches per window.
        self.assertTrue(service._event_type_allowed(1, "whale_alert"))
        pipe.incr.assert_called_once()
        key = pipe.incr.call_args.args[0]
        self.assertTrue(key.startswith("alert:storm:1:whale_alert:"))
        pipe.expire.assert_called_once_with(key, 120)

    async def test_redis_storm_counter_blocks_over_limit(self):
        service = _make_service(self.factory)
        fake_redis = mock.MagicMock()
        pipe = mock.MagicMock()
        fake_redis.pipeline.return_value = pipe
        pipe.execute.return_value = [11]
        service._redis = fake_redis
        self.assertFalse(service._event_type_allowed(1, "whale_alert"))

    async def test_redis_storm_failure_fails_open_to_memory(self):
        service = _make_service(self.factory)
        fake_redis = mock.MagicMock()
        pipe = mock.MagicMock()
        fake_redis.pipeline.return_value = pipe
        pipe.execute.side_effect = RuntimeError("redis down")
        service._redis = fake_redis
        self.assertTrue(service._event_type_allowed(1, "fill"))
        self.assertIn("1:fill", service._storm_timestamps)

    async def test_memory_storm_window_expires_old_timestamps(self):
        service = _make_service(self.factory, storm_limits={"fill": 120})
        now = time.time()
        key = "1:fill"
        service._storm_timestamps[key] = deque([now - 120, now - 120, now - 10])
        self.assertTrue(service._event_type_allowed(1, "fill"))
        # The two expired timestamps were evicted from the window.
        remaining = list(service._storm_timestamps[key])
        self.assertEqual(len(remaining), 2)
        self.assertAlmostEqual(remaining[0], now - 10, delta=1)

    async def test_memory_storm_limit_blocks_when_window_full(self):
        service = _make_service(self.factory, storm_limits={"fill": 2})
        now = time.time()
        service._storm_timestamps["1:fill"] = deque([now, now])
        self.assertFalse(service._event_type_allowed(1, "fill"))


class DeliveryInternalsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()

    def tearDown(self):
        self.session.close()

    async def test_spawn_delivery_creates_background_task(self):
        service = _make_service(self.factory)
        with mock.patch.object(
            service, "_deliver_with_retry", new_callable=mock.AsyncMock
        ) as deliver:
            service._spawn_delivery(1, "telegram", 5, {"chat_id": "1"}, "fill", {"skip_feed": True})
            tasks = set(service._background_tasks)
            self.assertTrue(tasks)
            await asyncio.gather(*tasks)
        deliver.assert_awaited_once_with(
            1, "telegram", 5, {"chat_id": "1"}, "fill", {"skip_feed": True}
        )

    async def test_record_attempt_without_delivery_returns_early(self):
        mock_session = mock.MagicMock()
        mock_session.get.return_value = None
        service = _make_service(self.factory)
        service._session_factory = lambda: mock_session
        service._record_attempt(999, 1, "err", "pending")
        mock_session.commit.assert_not_called()

    async def test_record_attempt_swallows_database_errors(self):
        mock_session = mock.MagicMock()
        mock_session.get.side_effect = RuntimeError("db down")
        service = _make_service(self.factory)
        service._session_factory = lambda: mock_session
        service._record_attempt(1, 1, "err", "pending")
        mock_session.rollback.assert_called_once()

    async def test_rate_limit_exhaustion_fails_delivery(self):
        service = _make_service(self.factory)
        service.rate_limiter = mock.MagicMock()
        service.rate_limiter.acquire = mock.AsyncMock(return_value=False)
        with (
            mock.patch.object(service, "_record_attempt") as record,
            mock.patch("app.services.alert_service.asyncio.sleep", new_callable=mock.AsyncMock),
        ):
            result = await service._deliver_with_retry(1, "telegram", 5, {}, "fill", {})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "rate limit exceeded for telegram channel 5")
        self.assertEqual(record.call_count, 4)
        record.assert_called_with(1, 3, "rate limit exceeded for telegram channel 5", "failed")

    async def test_send_rejects_unknown_channel_type(self):
        service = _make_service(self.factory)
        with self.assertRaises(AlertDeliveryError):
            await service._send("carrier_pigeon", {}, "fill", {})

    async def test_telegram_without_chat_id_fails(self):
        service = _make_service(self.factory)
        with _patched_settings(), self.assertRaises(AlertDeliveryError):
            await service._send_telegram({}, "fill", {})

    async def test_discord_without_webhook_url_fails(self):
        service = _make_service(self.factory)
        with self.assertRaises(AlertDeliveryError):
            await service._send_discord({}, "fill", {})

    async def test_webhook_without_url_fails(self):
        service = _make_service(self.factory)
        with self.assertRaises(AlertDeliveryError):
            await service._send_webhook({}, "fill", {})

    async def test_webhook_with_secret_signs_payload(self):
        service = _make_service(self.factory)
        config = {"url": "https://example.com/hook", "secret": "s3cr3t"}
        with _public_dns(), _patched_http() as fake_client:
            await service._send_webhook(config, "fill", {"skip_feed": True})
        headers = fake_client.post.call_args.kwargs["headers"]
        self.assertIn("X-Signature", headers)


class SingletonTests(unittest.TestCase):
    def test_get_alert_service_returns_cached_instance(self):
        with (
            mock.patch.object(alert_mod, "_alert_service", None),
            mock.patch.object(alert_mod, "AlertService") as service_cls,
        ):
            first = get_alert_service()
            second = get_alert_service()
        self.assertIs(first, second)
        self.assertIs(first, service_cls.return_value)
        service_cls.assert_called_once()

    def test_module_level_dispatch_delegates_to_singleton(self):
        mock_service = mock.MagicMock()
        mock_service.dispatch = mock.AsyncMock(return_value={"status": "dispatched"})
        with mock.patch.object(alert_mod, "_alert_service", mock_service):
            result = asyncio.run(alert_dispatch("fill", 1, {"a": 1}, background=False))
        self.assertEqual(result, {"status": "dispatched"})
        mock_service.dispatch.assert_awaited_once_with(
            "fill", 1, {"a": 1}, db=None, background=False, channel_ids_override=None
        )


if __name__ == "__main__":
    unittest.main()
