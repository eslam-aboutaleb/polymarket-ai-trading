"""Billing providers. The protocol lives here; Stripe is the first
implementation, and a future USDC/crypto provider implements the same
five methods without any caller change.

Import from ``app.services.billing`` rather than reaching into
``stripe_provider`` directly, so swapping the default provider is a
one-line change here rather than an edit in every caller.
"""

from __future__ import annotations

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

__all__ = [
    "BillingError",
    "BillingNotConfigured",
    "BillingProvider",
    "CancelResult",
    "CheckoutSession",
    "PortalSession",
    "StripeBillingProvider",
    "WebhookSignatureError",
    "billing_configured",
]
