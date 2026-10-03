"""Tests for the multi-channel alert dispatch service.

Covers each sender with mocked HTTP, exponential-backoff retry,
the per-channel token-bucket rate limiter (Redis and in-memory),
preference routing, digest mode, storm protection, the SSRF
guard, and Fernet encryption of channel config at rest.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import socket
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.notification_channel import NotificationChannel
from app.models.notification_delivery import NotificationDelivery
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.user_notification_preference import UserNotificationPreference
from app.security.crypto import decrypt_text, encrypt_text
from app.services.alert_service import (
    AlertDeliveryError,
    AlertService,
    TokenBucketRateLimiter,
    validate_webhook_url,
)


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


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@contextlib.contextmanager
def _public_dns():
    def _resolve(host, port, proto=0, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    with mock.patch("socket.getaddrinfo", side_effect=_resolve):
        yield


@contextlib.contextmanager
def _private_dns():
    def _resolve(host, port, proto=0, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    with mock.patch("socket.getaddrinfo", side_effect=_resolve):
        yield


def _fake_http_client(post_side_effect=None):
    response = mock.MagicMock()
    client = mock.AsyncMock()
    client.post.return_value = response
    if post_side_effect is not None:
        client.post.side_effect = post_side_effect
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


@contextlib.contextmanager
def _patched_http(post_side_effect=None):
    fake_client = _fake_http_client(post_side_effect)
    with mock.patch(
        "app.services.alert_service.httpx.AsyncClient",
        return_value=fake_client,
    ):
        yield fake_client


@contextlib.contextmanager
def _patched_settings(token=None):
    fake_settings = mock.MagicMock()
    fake_settings.telegram_bot_token = "123456:ABC-DEF" if token is None else token
    with mock.patch("app.services.alert_service.get_settings", return_value=fake_settings):
        yield fake_settings


class TokenBucketRateLimiterTests(unittest.TestCase):
    def test_in_memory_bucket_refills_over_time(self):
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(redis_url="", limits={"webhook": (1, 1.0)}, clock=clock)
        self.assertTrue(limiter.try_acquire("webhook", 1))
        self.assertFalse(limiter.try_acquire("webhook", 1))
        clock.advance(1.1)
        self.assertTrue(limiter.try_acquire("webhook", 1))

    def test_unknown_channel_type_uses_webhook_limit(self):
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(redis_url="", limits={"webhook": (1, 1.0)}, clock=clock)
        self.assertTrue(limiter.try_acquire("carrier_pigeon", 9))
        self.assertFalse(limiter.try_acquire("carrier_pigeon", 9))

    def test_redis_backend_consumes_tokens(self):
        fake_redis = mock.MagicMock()
        fake_redis.eval.return_value = 1
        limiter = TokenBucketRateLimiter(
            redis_url="", limits={"telegram": (30, 30.0)}, clock=FakeClock()
        )
        limiter._redis = fake_redis
        self.assertTrue(limiter.try_acquire("telegram", 5))
        fake_redis.eval.assert_called_once()
        fake_redis.eval.return_value = 0
        self.assertFalse(limiter.try_acquire("telegram", 5))
        self.assertEqual(fake_redis.eval.call_count, 2)

    def test_redis_failure_fails_open(self):
        fake_redis = mock.MagicMock()
        fake_redis.eval.side_effect = RuntimeError("redis down")
        limiter = TokenBucketRateLimiter(
            redis_url="", limits={"telegram": (30, 30.0)}, clock=FakeClock()
        )
        limiter._redis = fake_redis
        self.assertTrue(limiter.try_acquire("telegram", 5))

    def test_acquire_times_out_when_never_allowed(self):
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(redis_url="", limits={"webhook": (1, 1.0)}, clock=clock)
        self.assertTrue(limiter.try_acquire("webhook", 1))
        result = asyncio.run(limiter.acquire("webhook", 1, timeout=0.05))
        self.assertFalse(result)


class WebhookSecurityTests(unittest.TestCase):
    def test_rejects_non_https_scheme(self):
        with self.assertRaises(AlertDeliveryError):
            validate_webhook_url("http://example.com/hook")

    def test_rejects_url_without_host(self):
        with self.assertRaises(AlertDeliveryError):
            validate_webhook_url("https://")

    def test_blocks_loopback_address(self):
        with _private_dns(), self.assertRaises(AlertDeliveryError):
            validate_webhook_url("https://example.com/hook")

    def test_blocks_private_address(self):
        def _resolve(host, port, proto=0, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

        with (
            mock.patch("socket.getaddrinfo", side_effect=_resolve),
            self.assertRaises(AlertDeliveryError),
        ):
            validate_webhook_url("https://example.com/hook")

    def test_blocks_ipv6_loopback(self):
        def _resolve(host, port, proto=0, **kwargs):
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]

        with (
            mock.patch("socket.getaddrinfo", side_effect=_resolve),
            self.assertRaises(AlertDeliveryError),
        ):
            validate_webhook_url("https://example.com/hook")

    def test_allows_public_address(self):
        with _public_dns():
            validate_webhook_url("https://example.com/hook")

    def test_unresolvable_host_is_rejected(self):
        with (
            mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no dns")),
            self.assertRaises(AlertDeliveryError),
        ):
            validate_webhook_url("https://example.com/hook")


class ChannelEncryptionTests(unittest.TestCase):
    def test_channel_config_is_encrypted_at_rest(self):
        factory = _session_factory()
        session = factory()
        plaintext = json.dumps({"chat_id": "12345", "secret": "s3cr3t"})
        encrypted, _key_id = encrypt_text(plaintext)
        channel = NotificationChannel(
            user_id=1, channel_type="webhook", name="c", config_json=encrypted
        )
        session.add(channel)
        session.commit()
        session.expire_all()
        stored = session.get(NotificationChannel, channel.id)
        raw = stored.config_json
        self.assertTrue(raw.startswith("gAAAAA"))
        self.assertNotIn("12345", raw)
        self.assertNotIn("s3cr3t", raw)
        self.assertEqual(json.loads(decrypt_text(raw)), json.loads(plaintext))
        session.close()


class AlertServiceDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()

    def tearDown(self):
        self.session.close()

    async def test_telegram_sender_posts_to_bot_api(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http() as fake_client:
            result = await service.dispatch(
                "fill", 1, {"skip_feed": True, "market_id": "m1"}, background=False
            )
        self.assertEqual(result["status"], "dispatched")
        url = fake_client.post.call_args.args[0]
        self.assertEqual(url, "https://api.telegram.org/bot123456:ABC-DEF/sendMessage")
        body = fake_client.post.call_args.kwargs["json"]
        self.assertEqual(body["chat_id"], "12345")
        self.assertIn("Order Fill", body["text"])
        self.assertIn("m1", body["text"])

    async def test_discord_sender_posts_webhook(self):
        _add_channel(self.session, 1, "discord")
        service = _make_service(self.factory)
        with _public_dns(), _patched_http() as fake_client:
            result = await service.dispatch(
                "stop_loss_hit",
                1,
                {"skip_feed": True, "market_id": "m2"},
                background=False,
            )
        self.assertEqual(result["status"], "dispatched")
        url = fake_client.post.call_args.args[0]
        self.assertEqual(url, "https://discord.com/api/webhooks/1/abc")
        body = fake_client.post.call_args.kwargs["json"]
        self.assertIn("Stop Loss Hit", body["content"])

    async def test_webhook_sender_signs_with_hmac(self):
        config = {"url": "https://example.com/hook", "secret": "s3cr3t"}
        _add_channel(self.session, 1, "webhook", config=config)
        service = _make_service(self.factory)
        with _public_dns(), _patched_http() as fake_client:
            await service.dispatch("take_profit_hit", 1, {"skip_feed": True}, background=False)
        kwargs = fake_client.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        content = kwargs["content"]
        expected = hmac.new(b"s3cr3t", content, hashlib.sha256).hexdigest()
        self.assertEqual(kwargs["headers"]["X-Signature"], f"sha256={expected}")
        payload = json.loads(content)
        self.assertEqual(payload["event_type"], "take_profit_hit")

    async def test_webhook_sender_without_secret_omits_signature(self):
        _add_channel(self.session, 1, "webhook")
        service = _make_service(self.factory)
        with _public_dns(), _patched_http() as fake_client:
            await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        headers = fake_client.post.call_args.kwargs["headers"]
        self.assertNotIn("X-Signature", headers)

    async def test_retry_with_exponential_backoff(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        response = mock.MagicMock()
        with (
            _patched_settings(),
            _patched_http(post_side_effect=[Exception("boom"), Exception("boom"), response]),
            mock.patch(
                "app.services.alert_service.asyncio.sleep",
                new_callable=mock.AsyncMock,
            ) as sleep_mock,
        ):
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        self.assertEqual(result["status"], "dispatched")
        delivery = result["deliveries"][0]
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(delivery["attempts"], 3)
        sleep_mock.assert_has_awaits([mock.call(1.0), mock.call(2.0)])

    async def test_retry_exhaustion_marks_delivery_failed(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with (
            _patched_settings(),
            _patched_http(post_side_effect=Exception("boom")),
            mock.patch(
                "app.services.alert_service.asyncio.sleep",
                new_callable=mock.AsyncMock,
            ),
        ):
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        delivery = result["deliveries"][0]
        self.assertEqual(delivery["status"], "failed")
        self.assertIn("boom", delivery["error"])
        row = self.session.get(NotificationDelivery, delivery["delivery_id"])
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.attempts, 3)
        self.assertEqual(row.error, "boom")

    async def test_preference_routes_to_selected_channels(self):
        telegram = _add_channel(self.session, 1, "telegram")
        _add_channel(self.session, 1, "discord")
        preference = UserNotificationPreference(
            user_id=1,
            event_type="fill",
            channel_ids=json.dumps([telegram.id]),
            enabled=True,
        )
        self.session.add(preference)
        self.session.commit()
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http() as fake_client:
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(result["deliveries"]), 1)
        rows = self.session.query(NotificationDelivery).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].channel_id, telegram.id)
        self.assertIn("api.telegram.org", fake_client.post.call_args.args[0])

    async def test_disabled_preference_silences_event(self):
        _add_channel(self.session, 1, "telegram")
        preference = UserNotificationPreference(
            user_id=1,
            event_type="fill",
            channel_ids=json.dumps([]),
            enabled=False,
        )
        self.session.add(preference)
        self.session.commit()
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http() as fake_client:
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_channels")
        fake_client.post.assert_not_called()
        self.assertEqual(self.session.query(NotificationDelivery).count(), 0)

    async def test_default_routing_uses_all_active_channels(self):
        _add_channel(self.session, 1, "telegram")
        _add_channel(self.session, 1, "webhook")
        _add_channel(self.session, 1, "discord", is_active=False)
        service = _make_service(self.factory)
        with _patched_settings(), _public_dns(), _patched_http() as fake_client:
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(result["deliveries"]), 2)
        self.assertEqual(fake_client.post.call_count, 2)
        self.assertEqual(self.session.query(NotificationDelivery).count(), 2)

    async def test_dispatch_creates_in_app_feed_event(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http():
            await service.dispatch(
                "fill",
                1,
                {
                    "trader_wallet": "0xabc",
                    "market_id": "m1",
                    "side": "buy",
                    "size": 1.5,
                    "price": 2.0,
                },
                background=False,
            )
        events = self.session.query(NotificationFeedEvent).all()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_type, "fill")
        self.assertEqual(event.trader_wallet, "0xabc")
        self.assertEqual(event.market_id, "m1")
        self.assertIsNone(event.read_at)

    async def test_dispatch_links_existing_feed_event(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http():
            await service.dispatch(
                "followed_trader_activity",
                1,
                {"feed_event_id": 42},
                background=False,
            )
        self.assertEqual(self.session.query(NotificationFeedEvent).count(), 0)
        rows = self.session.query(NotificationDelivery).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_id, "42")

    async def test_digest_mode_batches_high_volume_events(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with _patched_settings(), _patched_http():
            result = await service.dispatch(
                "whale_alert", 1, {"trader_wallet": "0xwhale"}, background=False
            )
        self.assertEqual(result["status"], "buffered")
        self.assertEqual(self.session.query(NotificationDelivery).count(), 0)
        with _patched_settings(), _patched_http():
            flush_result = await service.flush_digest(1, "whale_alert")
            self.assertEqual(flush_result["status"], "dispatched")
            tasks = set(service._background_tasks)
            if tasks:
                await asyncio.wait(tasks)
        rows = self.session.query(NotificationDelivery).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "sent")
        payload = json.loads(rows[0].payload_json)
        self.assertTrue(payload["digest"])
        self.assertEqual(payload["count"], 1)

    async def test_event_type_storm_limit_skips_dispatch(self):
        service = _make_service(self.factory, storm_limits={"fill": 2})
        first = await service.dispatch("fill", 1, {"skip_feed": True})
        second = await service.dispatch("fill", 1, {"skip_feed": True})
        third = await service.dispatch("fill", 1, {"skip_feed": True})
        self.assertEqual(first["reason"], "no_channels")
        self.assertEqual(second["reason"], "no_channels")
        self.assertEqual(third["status"], "skipped")
        self.assertEqual(third["reason"], "event_type_rate_limited")

    async def test_telegram_without_bot_token_fails_delivery(self):
        _add_channel(self.session, 1, "telegram")
        service = _make_service(self.factory)
        with _patched_settings(token=""), _patched_http() as fake_client:
            result = await service.dispatch("fill", 1, {"skip_feed": True}, background=False)
        delivery = result["deliveries"][0]
        self.assertEqual(delivery["status"], "failed")
        self.assertIn("TELEGRAM_BOT_TOKEN", delivery["error"])
        fake_client.post.assert_not_called()

    async def test_unknown_event_type_is_rejected(self):
        service = _make_service(self.factory)
        with self.assertRaises(ValueError):
            await service.dispatch("not_an_event", 1, {})

    async def test_send_test_alert_targets_single_channel(self):
        telegram = _add_channel(self.session, 1, "telegram")
        _add_channel(self.session, 1, "webhook")
        service = _make_service(self.factory)
        with _patched_settings(), _public_dns(), _patched_http() as fake_client:
            result = await service.send_test_alert(1, channel_id=telegram.id)
        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(result["deliveries"]), 1)
        self.assertIn("api.telegram.org", fake_client.post.call_args.args[0])
        rows = self.session.query(NotificationDelivery).all()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0].payload_json)
        self.assertTrue(payload["test"])


if __name__ == "__main__":
    unittest.main()
