"""Billing providers. The protocol lives here; Stripe is the first
implementation, and a future USDC/crypto provider implements the same
five methods without any caller change.

Import from ``app.services.billing`` rather than reaching into
``stripe_provider`` directly, so swapping the default provider is a
one-line change here rather than an edit in every caller.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.config import get_settings
from app.services.billing.stripe_provider import (
    BillingError,
    BillingNotConfigured,
    BillingProvider,
    CancelResult,
    CheckoutSession,
    PortalSession,
    StripeBillingProvider,
    WebhookSignatureError,
    billing_configured,
)


class BillingProviderRegistry:
    """Thread-safe registry of billing provider factories."""

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], BillingProvider]] = {}
        self._lock = threading.Lock()

    def register(self, name: str, factory: Callable[[], BillingProvider]) -> None:
        with self._lock:
            self._providers[name.lower()] = factory

    def get(self, name: str) -> BillingProvider | None:
        with self._lock:
            factory = self._providers.get(name.lower())
            return factory() if factory else None

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._providers)


billing_registry = BillingProviderRegistry()
billing_registry.register("stripe", lambda: StripeBillingProvider())


def get_billing_provider(name: str | None = None) -> BillingProvider:
    """Factory function for resolving the active billing provider."""
    settings = get_settings()
    provider_name = (
        (name or getattr(settings, "billing_provider", None) or "stripe").strip().lower()
    )
    provider = billing_registry.get(provider_name)
    if provider is None:
        if provider_name == "stripe":
            return StripeBillingProvider()
        raise BillingNotConfigured(f"Unknown or unconfigured billing provider: '{provider_name}'")
    return provider


__all__ = [
    "BillingError",
    "BillingNotConfigured",
    "BillingProvider",
    "BillingProviderRegistry",
    "CancelResult",
    "CheckoutSession",
    "PortalSession",
    "StripeBillingProvider",
    "WebhookSignatureError",
    "billing_configured",
    "billing_registry",
    "get_billing_provider",
]
