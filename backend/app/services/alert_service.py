"""Multi-channel alert dispatch service.

Resolves per-user notification preferences, records in-app feed
events, enqueues per-channel deliveries with exponential-backoff
retry, and enforces per-channel token-bucket rate limits
(Redis-backed with an in-memory fallback) plus per-event-type
storm protection and a 5-minute digest mode for high-volume
event types such as ``whale_alert``.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.notification_channel import NotificationChannel
from app.models.notification_delivery import NotificationDelivery
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.user_notification_preference import UserNotificationPreference
from app.security.crypto import decrypt_text
from app.utils.database import SessionLocal
from app.utils.time import utc_now

try:
    import redis as redis_lib

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)

# Full alert event catalog.
ALERT_EVENT_TYPES = frozenset(
    {
        "fill",
        "stop_loss_hit",
        "take_profit_hit",
        "followed_trader_activity",
        "arbitrage_detected",
        "latency_arb_opportunity",
        "whale_alert",
        "redeem_available",
        "redeem_failed",
        "system_halt",
        "dead_man_switch",
        "drawdown_warning",
    }
)

EVENT_TYPE_LABELS = {
    "fill": "Order Fill",
    "stop_loss_hit": "Stop Loss Hit",
    "take_profit_hit": "Take Profit Hit",
    "followed_trader_activity": "Followed Trader Activity",
    "arbitrage_detected": "Arbitrage Detected",
    "latency_arb_opportunity": "Latency Arbitrage Opportunity",
    "whale_alert": "Whale Alert",
    "redeem_available": "Redemption Available",
    "redeem_failed": "Redemption Failed",
    "system_halt": "System Halt",
    "dead_man_switch": "Dead Man Switch",
    "drawdown_warning": "Drawdown Warning",
}

# High-volume event types buffered into a periodic digest instead
# of firing one alert per event.
DIGEST_EVENT_TYPES = frozenset({"whale_alert"})

# Max dispatches per (user, event_type) per 60-second window.
DEFAULT_EVENT_TYPE_LIMIT = 60
EVENT_TYPE_RATE_LIMITS = {
    "whale_alert": 10,
    "fill": 120,
    "system_halt": 10,
    "dead_man_switch": 5,
    "drawdown_warning": 20,
}

# Per-channel token buckets: (capacity, refill tokens per second).
# Telegram 30 msg/s, Discord 5 per 2s, generic webhook 1 msg/s.
DEFAULT_CHANNEL_LIMITS = {
    "telegram": (30, 30.0),
    "discord": (5, 2.5),
    "webhook": (1, 1.0),
}

MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0
RATE_LIMIT_WAIT_SECONDS = 5.0
STORM_WINDOW_SECONDS = 60


class AlertDeliveryError(Exception):
    """Raised when a single delivery attempt cannot proceed."""


@dataclass
class _MemoryBucket:
    """Process-local token bucket state."""

    tokens: float
    last_refill: float


def _connect_redis(redis_url: str) -> Any | None:
    """Return a pinged Redis client, or None when unavailable."""
    if not redis_url or not REDIS_AVAILABLE:
        return None
    try:
        client = redis_lib.from_url(redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        logger.warning(
            "Redis unavailable for alert service; using in-memory fallbacks.",
            exc_info=True,
        )
        return None


def validate_webhook_url(url: str) -> None:
    """SSRF guard for user-supplied webhook URLs.

    Requires the https scheme and blocks private, loopback,
    link-local, reserved, and multicast addresses after DNS
    resolution. Note: DNS rebinding (TOCTOU) between this check
    and the POST is a known limitation; the request still goes to
    the resolved hostname over TLS.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AlertDeliveryError("Webhook URL must use the https scheme")
    host = parsed.hostname
    if not host:
        raise AlertDeliveryError("Webhook URL has no host")
    try:
        addr_infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise AlertDeliveryError(f"Cannot resolve webhook host: {host}") from exc
    for addr_info in addr_infos:
        ip = ipaddress.ip_address(addr_info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise AlertDeliveryError(
                f"Webhook URL resolves to a blocked address ({addr_info[4][0]})"
            )


def format_alert_message(event_type: str, payload: dict[str, Any]) -> str:
    """Render a human-readable alert message for chat-style channels."""
    label = EVENT_TYPE_LABELS.get(event_type, event_type.replace("_", " ").title())
    if payload.get("digest"):
        count = payload.get("count", 0)
        return f"[Alert] {label} digest - {count} events batched"
    lines = [f"[Alert] {label}"]
    for key in ("message", "trader_wallet", "market_id", "side", "size", "price", "reason"):
        value = payload.get(key)
        if value is None or value == "":
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


class TokenBucketRateLimiter:
    """Per-channel token-bucket rate limiter.

    Bucket state lives in Redis (atomic Lua refill) when a Redis
    URL is configured; otherwise an in-memory bucket is used, which
    is safe under the platform's single-worker constraint.
    """

    _LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
    tokens = capacity
end
if ts == nil then
    ts = now
end
local delta = math.max(0, now - ts)
tokens = math.min(capacity, tokens + delta * rate)
local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil((capacity / rate) * 1000) + 1000)
return allowed
"""

    def __init__(
        self,
        redis_url: str | None = None,
        limits: dict[str, tuple[int, float]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._limits = limits if limits is not None else dict(DEFAULT_CHANNEL_LIMITS)
        self._clock = clock
        url = redis_url if redis_url is not None else get_settings().redis_url
        self._redis = _connect_redis(url)
        self._buckets: dict[str, _MemoryBucket] = {}
        self._lock = threading.Lock()

    def try_acquire(self, channel_type: str, channel_id: int) -> bool:
        """Consume one token without waiting."""
        capacity, rate = self._limits.get(channel_type, DEFAULT_CHANNEL_LIMITS["webhook"])
        key = f"alert:rl:{channel_type}:{channel_id}"
        if self._redis is not None:
            try:
                result = self._redis.eval(self._LUA_SCRIPT, 1, key, capacity, rate, self._clock())
                return bool(result)
            except Exception:
                logger.warning("Redis rate-limit eval failed; failing open.", exc_info=True)
                return True
        return self._try_acquire_memory(key, capacity, rate)

    def _try_acquire_memory(self, key: str, capacity: int, rate: float) -> bool:
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _MemoryBucket(tokens=float(capacity), last_refill=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(float(capacity), bucket.tokens + elapsed * rate)
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    async def acquire(
        self, channel_type: str, channel_id: int, timeout: float = RATE_LIMIT_WAIT_SECONDS
    ) -> bool:
        """Acquire a token, polling until available or timeout."""
        deadline = time.monotonic() + timeout
        while True:
            if self.try_acquire(channel_type, channel_id):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)


class AlertService:
    """Resolves preferences and dispatches alerts to user channels."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        redis_url: str | None = None,
        digest_interval_seconds: float = 300.0,
        retry_base_delay_seconds: float = RETRY_BASE_DELAY_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        http_timeout_seconds: float = 10.0,
        storm_limits: dict[str, int] | None = None,
    ):
        self._session_factory = session_factory or SessionLocal
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter()
        self._redis = _connect_redis(
            redis_url if redis_url is not None else get_settings().redis_url
        )
        self._digest_interval_seconds = digest_interval_seconds
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._max_attempts = max_attempts
        self._http_timeout_seconds = http_timeout_seconds
        self._storm_limits = (
            storm_limits if storm_limits is not None else dict(EVENT_TYPE_RATE_LIMITS)
        )
        self._background_tasks: set[asyncio.Task] = set()
        self._digest_buffer: dict[tuple[int, str], list[dict[str, Any]]] = {}
        self._digest_timers: dict[tuple[int, str], asyncio.TimerHandle] = {}
        self._storm_timestamps: dict[str, deque] = {}
        self._storm_lock = threading.Lock()

    async def dispatch(
        self,
        event_type: str,
        user_id: int,
        payload: dict[str, Any] | None = None,
        db: Session | None = None,
        background: bool = True,
        channel_ids_override: list[int] | None = None,
    ) -> dict[str, Any]:
        """Dispatch an alert event to a user's notification channels.

        Resolves the user's preference for ``event_type`` (falling
        back to all active channels when no preference row exists),
        records an in-app feed event, creates one pending
        NotificationDelivery row per target channel, and either
        spawns background delivery tasks or awaits delivery inline.
        """
        payload = dict(payload or {})
        if event_type not in ALERT_EVENT_TYPES:
            raise ValueError(f"Unknown alert event type: {event_type}")

        if not self._event_type_allowed(user_id, event_type):
            logger.warning("Alert storm protection skipped user=%s event=%s", user_id, event_type)
            return {"status": "skipped", "reason": "event_type_rate_limited"}

        if await self._maybe_digest(event_type, user_id, payload):
            return {"status": "buffered", "reason": "digest"}

        own_session = db is None
        session = db if db is not None else self._session_factory()
        deliveries: list[tuple[int, str, int, dict[str, Any]]] = []
        try:
            if channel_ids_override is not None:
                channel_ids = list(channel_ids_override)
            else:
                channel_ids = self._resolve_channel_ids(session, user_id, event_type)
            if not channel_ids:
                return {"status": "skipped", "reason": "no_channels"}

            feed_event_id = payload.get("feed_event_id")
            if not feed_event_id and not payload.get("skip_feed"):
                feed_event_id = self._create_feed_event(session, user_id, event_type, payload)

            channels = (
                session.query(NotificationChannel)
                .filter(
                    NotificationChannel.id.in_(channel_ids),
                    NotificationChannel.user_id == user_id,
                )
                .all()
            )
            for channel in channels:
                if not channel.is_active:
                    continue
                try:
                    config = json.loads(decrypt_text(channel.config_json))
                except Exception:
                    logger.exception(
                        "Failed to decrypt config for notification channel %s", channel.id
                    )
                    continue
                delivery = NotificationDelivery(
                    channel_id=channel.id,
                    event_id=str(payload.get("event_id") or feed_event_id or uuid.uuid4()),
                    event_type=event_type,
                    payload_json=json.dumps(payload),
                    status="pending",
                )
                session.add(delivery)
                session.flush()
                deliveries.append((delivery.id, channel.channel_type, channel.id, config))
            session.commit()
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

        if not deliveries:
            return {"status": "skipped", "reason": "no_active_channels"}

        if background:
            for delivery_id, channel_type, channel_id, config in deliveries:
                self._spawn_delivery(
                    delivery_id, channel_type, channel_id, config, event_type, payload
                )
            return {"status": "dispatched", "deliveries": [item[0] for item in deliveries]}

        results = []
        for delivery_id, channel_type, channel_id, config in deliveries:
            results.append(
                await self._deliver_with_retry(
                    delivery_id, channel_type, channel_id, config, event_type, payload
                )
            )
        return {"status": "dispatched", "deliveries": results}

    async def send_test_alert(
        self,
        user_id: int,
        channel_id: int | None = None,
        db: Session | None = None,
        message: str = "Test alert from Polymarket notification center",
    ) -> dict[str, Any]:
        """Send a test alert through one channel (or all active channels)."""
        payload = {
            "message": message,
            "test": True,
            "skip_feed": True,
            "event_id": f"test-{uuid.uuid4()}",
        }
        override = [channel_id] if channel_id is not None else None
        return await self.dispatch(
            "system_halt",
            user_id,
            payload,
            db=db,
            background=False,
            channel_ids_override=override,
        )

    async def flush_digest(
        self, user_id: int, event_type: str, db: Session | None = None
    ) -> dict[str, Any] | None:
        """Flush a buffered digest immediately (timer or manual trigger)."""
        key = (user_id, event_type)
        timer = self._digest_timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        events = self._digest_buffer.pop(key, [])
        if not events:
            return None
        aggregated: dict[str, Any] = {
            "event_type": event_type,
            "count": len(events),
            "events": events,
            "digest": True,
        }
        return await self.dispatch(event_type, user_id, aggregated, db=db, background=True)

    def _resolve_channel_ids(self, session: Session, user_id: int, event_type: str) -> list[int]:
        """Preference rows are authoritative; absence means all active channels."""
        preference = (
            session.query(UserNotificationPreference)
            .filter(
                UserNotificationPreference.user_id == user_id,
                UserNotificationPreference.event_type == event_type,
            )
            .first()
        )
        if preference is not None:
            if not preference.enabled:
                return []
            try:
                ids = json.loads(preference.channel_ids)
            except (TypeError, ValueError):
                logger.warning("Malformed channel_ids for preference %s", preference.id)
                return []
            channel_ids: list[int] = []
            for item in ids:
                try:
                    channel_ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            return channel_ids
        rows = (
            session.query(NotificationChannel.id)
            .filter(
                NotificationChannel.user_id == user_id,
                NotificationChannel.is_active.is_(True),
            )
            .all()
        )
        return [row.id for row in rows]

    def _create_feed_event(
        self, session: Session, user_id: int, event_type: str, payload: dict[str, Any]
    ) -> int:
        """Record the in-app alert-center event for this dispatch."""
        event = NotificationFeedEvent(
            user_id=user_id,
            trader_wallet=str(payload.get("trader_wallet") or ""),
            event_type=event_type,
            market_id=str(payload.get("market_id") or ""),
            token_id=str(payload.get("token_id") or ""),
            side=str(payload.get("side") or ""),
            size=float(payload.get("size") or 0.0),
            price=float(payload.get("price") or 0.0),
            email_status="skipped",
        )
        session.add(event)
        session.flush()
        return event.id

    async def _maybe_digest(self, event_type: str, user_id: int, payload: dict[str, Any]) -> bool:
        """Buffer high-volume events; returns True when buffered."""
        if event_type not in DIGEST_EVENT_TYPES or payload.get("digest"):
            return False
        key = (user_id, event_type)
        self._digest_buffer.setdefault(key, []).append(payload)
        if key not in self._digest_timers:
            loop = asyncio.get_running_loop()
            self._digest_timers[key] = loop.call_later(
                self._digest_interval_seconds,
                lambda: asyncio.create_task(self._flush_digest(user_id, event_type)),
            )
        return True

    async def _flush_digest(self, user_id: int, event_type: str) -> dict[str, Any] | None:
        """Timer callback body for digest delivery."""
        return await self.flush_digest(user_id, event_type)

    def _event_type_allowed(self, user_id: int, event_type: str) -> bool:
        """Per-event-type storm protection (sliding 60s window)."""
        limit = self._storm_limits.get(event_type, DEFAULT_EVENT_TYPE_LIMIT)
        now = time.time()
        if self._redis is not None:
            bucket = int(now // STORM_WINDOW_SECONDS)
            key = f"alert:storm:{user_id}:{event_type}:{bucket}"
            try:
                pipe = self._redis.pipeline()
                pipe.incr(key)
                pipe.expire(key, STORM_WINDOW_SECONDS * 2)
                count = pipe.execute()[0]
                return int(count) <= limit
            except Exception:
                logger.warning("Storm counter Redis failure; failing open.", exc_info=True)
        key = f"{user_id}:{event_type}"
        with self._storm_lock:
            timestamps = self._storm_timestamps.setdefault(key, deque())
            while timestamps and now - timestamps[0] > STORM_WINDOW_SECONDS:
                timestamps.popleft()
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            return True

    def _spawn_delivery(
        self,
        delivery_id: int,
        channel_type: str,
        channel_id: int,
        config: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(
            self._deliver_with_retry(
                delivery_id, channel_type, channel_id, config, event_type, payload
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _deliver_with_retry(
        self,
        delivery_id: int,
        channel_type: str,
        channel_id: int,
        config: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Deliver one alert with exponential-backoff retry (3 attempts)."""
        last_error = "delivery failed"
        for attempt in range(1, self._max_attempts + 1):
            if attempt > 1:
                delay = self._retry_base_delay_seconds * (2 ** (attempt - 2))
                await asyncio.sleep(delay)
            if not await self.rate_limiter.acquire(channel_type, channel_id):
                last_error = f"rate limit exceeded for {channel_type} channel {channel_id}"
                self._record_attempt(delivery_id, attempt, last_error, "pending")
                continue
            try:
                await self._send(channel_type, config, event_type, payload)
                self._record_attempt(delivery_id, attempt, None, "sent", sent=True)
                return {"delivery_id": delivery_id, "status": "sent", "attempts": attempt}
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                self._record_attempt(delivery_id, attempt, last_error, "pending")
        self._record_attempt(delivery_id, self._max_attempts, last_error, "failed")
        return {"delivery_id": delivery_id, "status": "failed", "error": last_error}

    def _record_attempt(
        self, delivery_id: int, attempts: int, error: str | None, status: str, sent: bool = False
    ) -> None:
        """Persist per-attempt delivery state in its own session."""
        session = self._session_factory()
        try:
            delivery = session.get(NotificationDelivery, delivery_id)
            if delivery is None:
                return
            delivery.attempts = attempts
            delivery.error = error
            delivery.status = status
            if sent:
                delivery.sent_at = utc_now()
            session.commit()
        except Exception:
            logger.exception("Failed to record delivery attempt for %s", delivery_id)
            session.rollback()
        finally:
            session.close()

    async def _send(
        self, channel_type: str, config: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> None:
        if channel_type == "telegram":
            await self._send_telegram(config, event_type, payload)
        elif channel_type == "discord":
            await self._send_discord(config, event_type, payload)
        elif channel_type == "webhook":
            await self._send_webhook(config, event_type, payload)
        else:
            raise AlertDeliveryError(f"Unknown channel type: {channel_type}")

    async def _post_json(
        self,
        url: str,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        async with httpx.AsyncClient(timeout=self._http_timeout_seconds) as client:
            response = await client.post(url, json=json_body, content=content, headers=headers)
            response.raise_for_status()

    async def _send_telegram(
        self, config: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> None:
        token = get_settings().telegram_bot_token.strip()
        if not token:
            raise AlertDeliveryError("TELEGRAM_BOT_TOKEN is not configured")
        chat_id = str(config.get("chat_id") or "").strip()
        if not chat_id:
            raise AlertDeliveryError("Telegram channel is missing chat_id")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = format_alert_message(event_type, payload)
        await self._post_json(url, json_body={"chat_id": chat_id, "text": text})

    async def _send_discord(
        self, config: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> None:
        webhook_url = str(config.get("webhook_url") or "").strip()
        if not webhook_url:
            raise AlertDeliveryError("Discord channel is missing webhook_url")
        validate_webhook_url(webhook_url)
        text = format_alert_message(event_type, payload)
        await self._post_json(webhook_url, json_body={"content": text})

    async def _send_webhook(
        self, config: dict[str, Any], event_type: str, payload: dict[str, Any]
    ) -> None:
        webhook_url = str(config.get("url") or "").strip()
        if not webhook_url:
            raise AlertDeliveryError("Webhook channel is missing url")
        validate_webhook_url(webhook_url)
        body = json.dumps({**payload, "event_type": event_type}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = str(config.get("secret") or "")
        if secret:
            signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Signature"] = f"sha256={signature}"
        await self._post_json(webhook_url, content=body, headers=headers)


_alert_service: AlertService | None = None
_alert_service_lock = threading.Lock()


def get_alert_service() -> AlertService:
    """Lazy singleton for the process-wide alert service."""
    global _alert_service
    if _alert_service is None:
        with _alert_service_lock:
            if _alert_service is None:
                _alert_service = AlertService()
    return _alert_service


async def dispatch(
    event_type: str,
    user_id: int,
    payload: dict[str, Any] | None = None,
    db: Session | None = None,
    background: bool = True,
    channel_ids_override: list[int] | None = None,
) -> dict[str, Any]:
    """Dispatch an alert event to a user's channels (see AlertService.dispatch)."""
    return await get_alert_service().dispatch(
        event_type,
        user_id,
        payload,
        db=db,
        background=background,
        channel_ids_override=channel_ids_override,
    )
