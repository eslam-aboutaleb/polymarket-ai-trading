"""Billing providers — the provider-agnostic core plus Stripe (plan 09).

The seam is :class:`BillingProvider`. Everything the application needs from
a payment provider is expressed in five methods, and every method either
moves data in and out of the provider or verifies a signature — **none of
them writes to the database**. Applying a result is
:meth:`EntitlementService.apply_billing_event`, which is the only writer of
the plan column. That is what makes adding a USDC provider later a matter
of producing the same :class:`~app.services.users.entitlement_service.BillingEvent`
dataclass, with no caller changes and no second place that can grant a
plan.

Security posture of the webhook path:

* the **raw body** is what gets signed, so the route reads
  ``await request.body()`` and never lets a body parser re-serialise it
  (a re-serialised body is a different byte string and a different HMAC);
* the signature is HMAC-SHA256 over ``"{timestamp}.{body}"`` under the
  webhook signing secret, compared with :func:`hmac.compare_digest`;
* the header may carry several ``v1=`` signatures (Stripe rotates
  secrets) and any one of them may match;
* the timestamp is checked against a tolerance so a captured request
  cannot be replayed indefinitely;
* an **unconfigured** signing secret fails closed: no secret means no
  accepted webhook, never "skip verification";
* the endpoint carries **no other auth dependency**. The signature is the
  auth; adding a session dependency would make the endpoint unusable to
  the provider and would imply a second, weaker gate.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from app.config import get_settings
from app.services.users.entitlement_service import (
    BillingEvent,
    PlanTier,
    Subscription,
    SubscriptionStatus,
)

logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    """Base class for provider failures the route maps to a 5xx."""


class BillingNotConfigured(BillingError):
    """The provider has no credentials configured."""


class WebhookSignatureError(BillingError):
    """A webhook whose signature, timestamp or shape cannot be trusted."""


@dataclass(frozen=True)
class CheckoutSession:
    """A hosted checkout URL the browser is redirected to."""

    id: str
    url: str


@dataclass(frozen=True)
class PortalSession:
    """A hosted billing-portal URL for self-service changes."""

    id: str
    url: str


@dataclass(frozen=True)
class CancelResult:
    """What the provider did in response to a cancellation."""

    cancel_at_period_end: bool
    current_period_end: datetime | None = None


@runtime_checkable
class BillingProvider(Protocol):
    """Everything the application needs from a payment provider.

    Deliberately narrow: creating a customer, a checkout session and a
    portal session, cancelling, and turning a signed webhook into a
    :class:`BillingEvent`. Nothing here touches the database, so a second
    implementation cannot become a second writer of the plan column.
    """

    def create_customer(self, *, email: str, account_id: int) -> str:
        """Return the provider's customer id for ``email``."""
        ...

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        plan: str,
        account_id: int,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        """Return a hosted checkout session for ``plan``."""
        ...

    def create_portal_session(self, *, customer_id: str, return_url: str) -> PortalSession:
        """Return a hosted portal session for self-service management."""
        ...

    def handle_webhook(self, *, payload: bytes, signature: str | None) -> BillingEvent:
        """Verify a webhook and normalise it.

        Synchronous and pure apart from the provider's public key material:
        the caller needs to be able to reject a forged payload without an
        event loop, a database or a clock skew window.
        """
        ...

    async def cancel(self, *, subscription: Subscription, at_period_end: bool) -> CancelResult:
        """Cancel at the period boundary, or immediately."""
        ...


# ── Configuration access ────────────────────────────────────────
# Read through ``getattr`` so this module imports and works before the
# settings land in ``app/config.py`` — the same rollout order the API-key
# hash secret used. An absent value behaves as "not configured", never as
# a default secret.

_STRIPE_API_BASE = "https://api.stripe.com/v1"

#: How old a webhook timestamp may be, in seconds. Generous enough for a
#: provider retry loop, tight enough that a captured request is useless
#: after a few minutes.
_WEBHOOK_TOLERANCE_SECONDS = 300


def _setting(name: str, default: str = "") -> str:
    value = getattr(get_settings(), name, default)
    return str(value or "").strip()


def _stripe_secret_key() -> str:
    return _setting("stripe_secret_key")


def _stripe_webhook_secret() -> str:
    return _setting("stripe_webhook_secret")


def _plan_for_price(price_id: str | None) -> str | None:
    """Map a Stripe price id onto a plan tier, or ``None`` if unknown.

    Fail-closed by construction: a price id that is not configured maps to
    ``None``, and the normaliser turns ``None`` into "no plan change", so a
    new price created in the Stripe dashboard cannot silently grant
    anything.
    """
    if not price_id:
        return None
    if price_id == _setting("stripe_price_pro"):
        return PlanTier.PRO.value
    if price_id == _setting("stripe_price_institutional"):
        return PlanTier.INSTITUTIONAL.value
    return None


def _price_for_plan(plan: str) -> str:
    tier = PlanTier.coerce(plan)
    if tier is PlanTier.PRO:
        return _setting("stripe_price_pro")
    if tier is PlanTier.INSTITUTIONAL:
        return _setting("stripe_price_institutional")
    raise BillingNotConfigured(f"No Stripe price is configured for plan '{plan}'.")


def billing_configured() -> bool:
    """Whether a checkout URL can be produced at all.

    False when no Stripe secret or price is configured, which is the state
    a fresh deployment ships in — the endpoint then answers 503 with a
    clear message instead of a Stripe error the user cannot act on.
    """
    return bool(
        _stripe_secret_key()
        and _setting("stripe_price_pro")
        and _setting("stripe_price_institutional")
    )


# ── Status / time mapping ───────────────────────────────────────

#: Stripe subscription status → our status enum. ``unpaid`` collapses to
#: ``incomplete``: both mean the provider never completed the
#: subscription, and both must resolve to the free tier.
_STATUS_MAP = {
    "active": SubscriptionStatus.ACTIVE.value,
    "trialing": SubscriptionStatus.TRIALING.value,
    "past_due": SubscriptionStatus.PAST_DUE.value,
    "canceled": SubscriptionStatus.CANCELED.value,
    "incomplete": SubscriptionStatus.INCOMPLETE.value,
    "incomplete_expired": SubscriptionStatus.INCOMPLETE.value,
    "unpaid": SubscriptionStatus.INCOMPLETE.value,
    "paused": SubscriptionStatus.CANCELED.value,
}


def _map_status(raw: object) -> str:
    return _STATUS_MAP.get(str(raw or "").strip().lower(), SubscriptionStatus.INCOMPLETE.value)


def _to_datetime(raw: object) -> datetime | None:
    """Stripe sends epoch seconds; a string is parsed for robustness."""
    if raw in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _price_id_of(subscription: dict[str, Any]) -> str | None:
    items = subscription.get("items")
    if isinstance(items, dict):
        data = items.get("data")
        if isinstance(data, list) and data:
            price = data[0].get("price") if isinstance(data[0], dict) else None
            if isinstance(price, dict):
                return str(price.get("id") or "") or None
            if isinstance(price, str):
                return price
    return None


class StripeBillingProvider:
    """Stripe implementation of :class:`BillingProvider`.

    Talks to the REST API with ``httpx`` rather than the ``stripe`` SDK:
    the SDK is not a backend dependency, and the four calls this needs are
    form-encoded POSTs whose only interesting part is the response id.
    """

    def __init__(self, *, secret_key: str | None = None, webhook_secret: str | None = None):
        self._secret_key = (secret_key or _stripe_secret_key()).strip()
        self._webhook_secret = (webhook_secret or _stripe_webhook_secret()).strip()

    # ── Transport ──

    def _post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._secret_key:
            raise BillingNotConfigured("STRIPE_SECRET_KEY is not configured.")
        import httpx

        try:
            response = httpx.post(
                f"{_STRIPE_API_BASE}{path}",
                data=params,
                headers={
                    "Authorization": f"Bearer {self._secret_key}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise BillingError(f"Stripe request failed: {exc}") from exc
        if response.status_code >= 400:
            # The provider's message is the actionable part; the status
            # code decides which side is at fault.
            raise BillingError(
                f"Stripe rejected {path} with {response.status_code}: {response.text[:500]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise BillingError(f"Stripe returned a non-JSON body for {path}") from exc

    # ── Protocol ──

    def create_customer(self, *, email: str, account_id: int) -> str:
        payload = self._post(
            "/customers",
            {
                "email": email,
                # Looks the customer up by this pair on a retry, so a
                # double-submitted checkout cannot create two customers
                # for one account.
                "metadata[account_id]": str(account_id),
                "metadata[app]": "polymarket",
            },
        )
        customer_id = str(payload.get("id") or "")
        if not customer_id:
            raise BillingError("Stripe did not return a customer id")
        return customer_id

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        plan: str,
        account_id: int,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutSession:
        price_id = _price_for_plan(plan)
        payload = self._post(
            "/checkout/sessions",
            {
                "mode": "subscription",
                "customer": customer_id,
                # The plan and the account ride on the session metadata so
                # the completion webhook can resolve both without a second
                # lookup — and so an unknown price id can never be the
                # thing that decides the tier.
                "metadata[account_id]": str(account_id),
                "metadata[plan]": PlanTier.coerce(plan).value,
                "subscription_data[metadata][account_id]": str(account_id),
                "subscription_data[metadata][plan]": PlanTier.coerce(plan).value,
                "line_items[0][price]": price_id,
                "line_items[0][quantity]": "1",
                "success_url": success_url,
                "cancel_url": cancel_url,
            },
        )
        session_id = str(payload.get("id") or "")
        url = str(payload.get("url") or "")
        if not session_id or not url:
            raise BillingError("Stripe did not return a checkout session")
        return CheckoutSession(id=session_id, url=url)

    def create_portal_session(self, *, customer_id: str, return_url: str) -> PortalSession:
        payload = self._post(
            "/billing_portal/sessions",
            {"customer": customer_id, "return_url": return_url},
        )
        portal_id = str(payload.get("id") or "")
        url = str(payload.get("url") or "")
        if not portal_id or not url:
            raise BillingError("Stripe did not return a portal session")
        return PortalSession(id=portal_id, url=url)

    async def cancel(self, *, subscription: Subscription, at_period_end: bool) -> CancelResult:
        provider_id = subscription.provider_subscription_id
        if not provider_id:
            # Nothing at the provider to cancel: the local row already is
            # the whole subscription.
            return CancelResult(cancel_at_period_end=True)

        if not at_period_end:
            self._create_idempotent_delete(provider_id)
        else:
            self._post(
                f"/subscriptions/{provider_id}",
                {"cancel_at_period_end": "true"},
            )
        return CancelResult(
            cancel_at_period_end=True,
            current_period_end=subscription.current_period_end,
        )

    def _create_idempotent_delete(self, provider_subscription_id: str) -> None:
        """Cancel immediately, tolerating an already-cancelled subscription."""
        if not self._secret_key:
            raise BillingNotConfigured("STRIPE_SECRET_KEY is not configured.")
        import httpx

        try:
            response = httpx.delete(
                f"{_STRIPE_API_BASE}/subscriptions/{provider_subscription_id}",
                headers={"Authorization": f"Bearer {self._secret_key}"},
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise BillingError(f"Stripe cancellation failed: {exc}") from exc
        if response.status_code >= 400 and response.status_code != 404:
            raise BillingError(
                f"Stripe rejected the cancellation with {response.status_code}: "
                f"{response.text[:500]}"
            )

    # ── Webhook ──

    def verify_signature(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        """Verify and parse a webhook, returning the raw event object.

        Raises :class:`WebhookSignatureError` for anything that is not a
        well-formed, freshly-signed event from a holder of the signing
        secret: a bad header shape, an unknown signature version, an
        expired timestamp, a mismatched HMAC or a body that is not JSON.
        """
        if not self._webhook_secret:
            # Fail closed. An unconfigured secret must not mean "accept
            # everything"; that is how a forged webhook grants a plan.
            raise WebhookSignatureError("Billing webhook secret is not configured.")

        if not signature:
            raise WebhookSignatureError("Missing Stripe-Signature header")

        timestamp, signatures = _parse_signature_header(signature)
        if timestamp is None or not signatures:
            raise WebhookSignatureError("Malformed Stripe-Signature header")

        age = time.time() - timestamp
        if age > _WEBHOOK_TOLERANCE_SECONDS:
            raise WebhookSignatureError("Webhook timestamp is outside the tolerance window")

        signed_payload = f"{timestamp}.".encode() + payload
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise WebhookSignatureError("Webhook signature does not match")

        try:
            event = _json_loads(payload)
        except ValueError as exc:
            raise WebhookSignatureError("Webhook body is not valid JSON") from exc
        if not isinstance(event, dict) or not event.get("id"):
            raise WebhookSignatureError("Webhook body is not a Stripe event object")
        return event

    def handle_webhook(self, *, payload: bytes, signature: str | None) -> BillingEvent:
        event = self.verify_signature(payload, signature)
        return _normalize(event)


def _json_loads(payload: bytes) -> Any:
    import json

    return json.loads(payload.decode("utf-8"))


def _parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    """Split ``t=...,v1=...,v1=...`` into its timestamp and signatures."""
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key in ("v1", "v0") and value:
            signatures.append(value)
    return timestamp, signatures


def _normalize(event: dict[str, Any]) -> BillingEvent:
    """Turn a verified Stripe event into a provider-agnostic event.

    Only the four shapes that change a plan are mapped. Everything else
    becomes a no-op event carrying its id, which the ledger still records:
    an unmapped event type is never a reason to fail the request, because
    a provider retries on any non-2xx and a permanently failing endpoint
    stops delivering the events that do matter.
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    obj = obj if isinstance(obj, dict) else {}

    base = {
        "event_id": event_id,
        "event_type": event_type,
        "provider": "stripe",
    }

    if event_type == "checkout.session.completed":
        plan = _coerce_plan(obj.get("metadata", {}).get("plan"))
        base.update(
            {
                "account_id": _int_or_none(obj.get("metadata", {}).get("account_id")),
                "plan": plan,
                "provider_customer_id": _str_or_none(obj.get("customer")),
                "provider_subscription_id": _str_or_none(obj.get("subscription")),
                "status": SubscriptionStatus.ACTIVE.value,
            }
        )
        return BillingEvent(**base)

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        deleted = event_type.endswith("deleted")
        plan = None if deleted else _plan_for_price(_price_id_of(obj))
        if plan is None:
            # An unknown price id, or a deletion: neither grants a tier.
            plan = PlanTier.FREE.value if deleted else None
        status = SubscriptionStatus.CANCELED.value if deleted else _map_status(obj.get("status"))
        base.update(
            {
                "account_id": _int_or_none(obj.get("metadata", {}).get("account_id")),
                "plan": plan,
                "provider_customer_id": _str_or_none(obj.get("customer")),
                "provider_subscription_id": _str_or_none(obj.get("id")),
                "current_period_end": _to_datetime(obj.get("current_period_end")),
                "cancel_at_period_end": bool(obj.get("cancel_at_period_end")),
                "status": status,
            }
        )
        return BillingEvent(**base)

    if event_type == "invoice.payment_failed":
        base.update(
            {
                "account_id": None,
                "provider_customer_id": _str_or_none(obj.get("customer")),
                "provider_subscription_id": _str_or_none(obj.get("subscription")),
                "status": SubscriptionStatus.PAST_DUE.value,
            }
        )
        return BillingEvent(**base)

    # Unmapped: recorded, applied to nothing.
    return BillingEvent(**base)


def _coerce_plan(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in (PlanTier.PRO.value, PlanTier.INSTITUTIONAL.value):
        return text
    return None


def _str_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
