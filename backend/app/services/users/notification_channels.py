"""Notification channel adapter contract and registry.

Implements the Gateway/Adapter and Registry patterns for alert delivery channels.
Every notification channel (Telegram, Discord, Webhook, Email, etc.) is isolated
in an adapter conforming to the ``NotificationChannelAdapter`` protocol.

New channels can be plugged in by registering them into the ``channel_registry``
without modifying ``AlertService``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from app.config import get_settings
from app.services.users.notification_errors import AlertDeliveryError

logger = logging.getLogger(__name__)

PostJsonFn = Callable[..., Awaitable[None]]


@runtime_checkable
class NotificationChannelAdapter(Protocol):
    """Protocol for a pluggable notification channel adapter."""

    name: str

    async def send(
        self,
        config: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        post_json: PostJsonFn,
        format_message: Callable[[str, dict[str, Any]], str],
        validate_url: Callable[[str], None],  # noqa: ARG002 - shared adapter protocol
        bot_token: str | None = None,
    ) -> None:
        """Deliver the alert through the specific channel."""
        ...


class TelegramChannelAdapter:
    """Telegram bot notification channel."""

    name = "telegram"

    async def send(
        self,
        config: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        post_json: PostJsonFn,
        format_message: Callable[[str, dict[str, Any]], str],
        validate_url: Callable[[str], None],  # noqa: ARG002 - fixed Telegram endpoint
        bot_token: str | None = None,
    ) -> None:
        token = (bot_token if bot_token is not None else get_settings().telegram_bot_token).strip()
        if not token:
            raise AlertDeliveryError("TELEGRAM_BOT_TOKEN is not configured")
        chat_id = str(config.get("chat_id") or "").strip()
        if not chat_id:
            raise AlertDeliveryError("Telegram channel is missing chat_id")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = format_message(event_type, payload)
        await post_json(url, json_body={"chat_id": chat_id, "text": text})


class DiscordChannelAdapter:
    """Discord incoming webhook channel."""

    name = "discord"

    async def send(
        self,
        config: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        post_json: PostJsonFn,
        format_message: Callable[[str, dict[str, Any]], str],
        validate_url: Callable[[str], None],
        bot_token: str | None = None,  # noqa: ARG002 - channel has its own webhook
    ) -> None:
        webhook_url = str(config.get("webhook_url") or "").strip()
        if not webhook_url:
            raise AlertDeliveryError("Discord channel is missing webhook_url")
        validate_url(webhook_url)
        text = format_message(event_type, payload)
        await post_json(webhook_url, json_body={"content": text})


class WebhookChannelAdapter:
    """Generic HMAC-signed HTTP webhook channel."""

    name = "webhook"

    async def send(
        self,
        config: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        post_json: PostJsonFn,
        format_message: Callable[[str, dict[str, Any]], str],  # noqa: ARG002 - sends JSON
        validate_url: Callable[[str], None],
        bot_token: str | None = None,  # noqa: ARG002 - channel has its own secret
    ) -> None:
        webhook_url = str(config.get("url") or "").strip()
        if not webhook_url:
            raise AlertDeliveryError("Webhook channel is missing url")
        validate_url(webhook_url)
        body = json.dumps({**payload, "event_type": event_type}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = str(config.get("secret") or "")
        if secret:
            signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Signature"] = f"sha256={signature}"
        await post_json(webhook_url, content=body, headers=headers)


class NotificationChannelRegistry:
    """Thread-safe registry of notification channel adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, NotificationChannelAdapter] = {}
        self._lock = threading.Lock()

    def register(self, name: str, adapter: NotificationChannelAdapter) -> None:
        with self._lock:
            self._adapters[name.lower()] = adapter

    def get(self, name: str) -> NotificationChannelAdapter | None:
        with self._lock:
            return self._adapters.get(name.lower())

    def all(self) -> list[NotificationChannelAdapter]:
        with self._lock:
            return [self._adapters[k] for k in sorted(self._adapters)]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._adapters)


# Process-wide registry preloaded with standard channels
channel_registry = NotificationChannelRegistry()
channel_registry.register("telegram", TelegramChannelAdapter())
channel_registry.register("discord", DiscordChannelAdapter())
channel_registry.register("webhook", WebhookChannelAdapter())
