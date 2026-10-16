"""Billing routes — subscription state, checkout, portal and the webhook.

Four endpoints, three of them session-authenticated and one deliberately
not:

* ``GET  /api/billing/subscription`` — the plan, the usage-versus-quota
  bars and the plan matrix the UI renders.
* ``GET  /api/billing/plans`` — the static tier/capability matrix, so the
  UI never hard-codes what a plan includes.
* ``POST /api/billing/checkout-session`` — start a Stripe Checkout for an
  upgrade. The only place an email is required.
* ``POST /api/billing/portal-session`` — hand the customer to Stripe's
  hosted portal to downgrade, cancel or update a card.
* ``POST /api/billing/webhook`` — the provider's callback. **No auth
  dependency**: the HMAC signature is the auth, and there is no cookie,
  bearer token or API key on this path. Anything else would be a second,
  weaker gate in front of a first strong one.

The email rule is the plan's own: ``User.email`` is nullable, non-unique
and unvalidated today, so the free tier must never require one. It is
required here, at the single moment it is actually needed (a paid
subscription needs a receipt and a Stripe customer), and is validated
before it is stored so a wallet-only user is not silently created as a
Stripe customer with a garbage address.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_active_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.billing.subscription import PlanTier
from app.models.users.account import Account
from app.services.billing import (
    BillingError,
    BillingNotConfigured,
    BillingProvider,
    CheckoutSession,
    WebhookSignatureError,
    billing_configured,
    get_billing_provider,
)
from app.services.billing.account_service import (
    get_account_owner,
    update_account_owner_email,
)
from app.services.users.entitlement_service import (
    PLAN_CAPABILITIES,
    PLAN_LABELS,
    PLAN_ORDER,
    EntitlementService,
    is_valid_email,
    plan_limits,
)
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])

#: Plans a customer may buy through Checkout. ``free`` is absent on
#: purpose: there is nothing to charge for, and letting a caller "buy"
#: free would be a way to write the plan column outside the webhook.
PURCHASABLE_PLANS: tuple[str, ...] = (PlanTier.PRO.value, PlanTier.INSTITUTIONAL.value)

MAX_URL_LENGTH = 2048


# ── Schemas ─────────────────────────────────────────────────────


class CheckoutSessionRequest(BaseModel):
    """Start (or restart) a checkout for ``plan``."""

    plan: str = Field(..., description="pro or institutional")
    email: str | None = Field(
        default=None,
        description="Required when the account has no email on file",
    )
    success_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    cancel_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)


class CheckoutSessionResponse(BaseModel):
    """Where to send the browser."""

    url: str
    session_id: str
    plan: str


class PortalSessionResponse(BaseModel):
    """Where to send the browser for self-service management."""

    url: str


class WebhookResponse(BaseModel):
    """Always 200 for a verified event, replayed or not."""

    received: bool
    event_id: str
    event_type: str
    duplicate: bool
    applied: bool
    account_id: int | None = None
    plan: str | None = None


# ── Helpers ─────────────────────────────────────────────────────


def _provider() -> BillingProvider:
    return get_billing_provider()


def _frontend_origin(request: Request) -> str:
    """The origin to return the browser to after checkout.

    Prefers the configured ``PUBLIC_APP_ORIGIN`` so the redirect target is a
    trusted origin and not derived from the client-supplied ``Host`` header
    (which is attacker-controlled and would defeat
    :func:`_same_origin_redirect`). Falls back to the request only for
    local/dev where nothing is configured. The path is appended by the
    caller; only the scheme + host are taken.
    """
    configured = (get_settings().public_app_origin or "").strip()
    if configured:
        return configured.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def _same_origin_redirect(url: str | None, origin: str, default_path: str) -> str:
    """Accept a client redirect only when it stays on our own origin.

    A foreign ``success``/``cancel`` URL handed to Stripe is an open
    redirect: after checkout the browser returns to an attacker-controlled
    host (phishing). Anything that is not an absolute URL on the same
    scheme+host as ``origin`` silently falls back to our own settings page.
    """
    if url:
        try:
            parsed = urlparse(url)
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.scheme
            and parsed.netloc
            and f"{parsed.scheme}://{parsed.netloc}" == origin
        ):
            return url
    return f"{origin}{default_path}"


def _resolve_email(db: Session, account: Account, requested: str | None) -> str:
    """Return a valid email for the account, storing a newly supplied one.

    ``None`` is a legitimate answer only when the account already has a
    valid email on file: a paid subscription needs a receipt, a free
    account does not.
    """
    user = get_account_owner(db, account.owner_user_id)
    stored = getattr(user, "email", None) if user is not None else None
    if is_valid_email(stored):
        return str(stored).strip()

    if not is_valid_email(requested):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "email_required",
                "message": (
                    "A valid email address is required before subscribing, so Stripe can "
                    "issue receipts. The free tier does not need one."
                ),
                "field": "email",
            },
        )

    email = str(requested).strip()
    if user is not None:
        update_account_owner_email(db, user, email)
        logger.info(
            "Stored email for user_id=%s ahead of checkout for account_id=%s",
            account.owner_user_id,
            account.id,
        )
    return email


# ── Routes ──────────────────────────────────────────────────────


@router.get("/plans")
def list_plans(
    current_user: dict = Depends(get_current_user_from_token),
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """The tier matrix and the caller's current position in it.

    Authenticated rather than public because it also reports which plan
    the caller is on, and an unauthenticated read of that is an
    account-existence oracle for anybody with the URL.
    """
    service = EntitlementService(db)
    plans = []
    for plan in PLAN_ORDER:
        limits = plan_limits(plan)
        plans.append(
            {
                "plan": plan,
                "label": PLAN_LABELS.get(plan, plan),
                "capabilities": sorted(PLAN_CAPABILITIES[plan]),
                "limits": {
                    "max_accounts": limits.max_accounts,
                    "max_followed_traders": limits.max_followed_traders,
                    "quotas": limits.quotas,
                },
                "purchasable": plan in PURCHASABLE_PLANS,
            }
        )
    return {
        "plans": plans,
        "current_plan": service.effective_plan(account),
        "email_on_file": _email_on_file(db, account),
        "billing_configured": billing_configured(),
    }


def _email_on_file(db: Session, account: Account) -> bool:
    user = get_account_owner(db, account.owner_user_id)
    return bool(user is not None and is_valid_email(user.email))


@router.get("/subscription")
def get_subscription(
    current_user: dict = Depends(get_current_user_from_token),
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """The current plan plus usage against every quota and account ceiling."""
    service = EntitlementService(db)
    row = service.subscription_for(account)
    report = service.usage_report(account)
    return {
        "subscription": {
            "plan": row.plan,
            "status": row.status,
            "provider": row.provider,
            "current_period_end": row.current_period_end.isoformat()
            if row.current_period_end
            else None,
            "cancel_at_period_end": row.cancel_at_period_end,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        },
        "usage": report,
        "email_on_file": _email_on_file(db, account),
        "billing_configured": billing_configured(),
    }


@router.post("/checkout-session", response_model=CheckoutSessionResponse)
def create_checkout_session(
    body: CheckoutSessionRequest,
    request: Request,
    current_user: dict = Depends(get_current_user_from_token),
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """Create a Stripe Checkout session for an upgrade.

    Idempotent in the only way that matters here: the plan is written by
    the completion *webhook*, never by this endpoint, so a caller that
    abandons the checkout (or replays this POST) has changed nothing. The
    email, if supplied, is the one side effect and it is what Stripe needs
    to attach a customer.
    """
    plan = str(body.plan or "").strip().lower()
    if plan not in PURCHASABLE_PLANS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan must be one of {list(PURCHASABLE_PLANS)}",
        )

    service = EntitlementService(db)
    row = service.subscription_for(account)
    email = _resolve_email(db, account, body.email)

    customer_id = row.provider_customer_id
    if not customer_id:
        try:
            customer_id = _provider().create_customer(email=email, account_id=account.id)
        except BillingNotConfigured as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Billing is not configured on this deployment",
            ) from exc
        except BillingError as exc:
            logger.exception("Stripe customer creation failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not create a billing customer",
            ) from exc
        service.attach_provider_customer(row, customer_id)

    origin = _frontend_origin(request)
    success_url = _same_origin_redirect(
        body.success_url, origin, "/settings?tab=billing&checkout=success"
    )
    cancel_url = _same_origin_redirect(
        body.cancel_url, origin, "/settings?tab=billing&checkout=cancelled"
    )

    try:
        session: CheckoutSession = _provider().create_checkout_session(
            customer_id=customer_id,
            plan=plan,
            account_id=account.id,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except BillingNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Billing is not configured on this deployment: set the Stripe price "
                "for the requested plan."
            ),
        ) from exc
    except BillingError as exc:
        logger.exception("Stripe checkout session creation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start the checkout session",
        ) from exc

    logger.info(
        "Checkout session %s created for account_id=%s plan=%s",
        session.id,
        account.id,
        plan,
    )
    return CheckoutSessionResponse(url=session.url, session_id=session.id, plan=plan)


@router.post("/portal-session", response_model=PortalSessionResponse)
def create_portal_session(
    request: Request,
    return_url: str | None = None,
    current_user: dict = Depends(get_current_user_from_token),
    account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """Hand a subscriber to Stripe's hosted portal.

    The portal is where downgrades and cancellations happen: the plan
    column is written by the resulting webhook, so a self-service change
    cannot diverge from what the provider thinks.
    """
    row = EntitlementService(db).subscription_for(account)
    if not row.provider_customer_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "no_billing_customer",
                "message": "This account has no billing customer yet. Start a subscription first.",
            },
        )

    origin = _frontend_origin(request)
    try:
        portal = _provider().create_portal_session(
            customer_id=row.provider_customer_id,
            return_url=return_url or f"{origin}/settings?tab=billing",
        )
    except BillingNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured on this deployment",
        ) from exc
    except BillingError as exc:
        logger.exception("Stripe portal session creation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start the billing portal session",
        ) from exc

    return PortalSessionResponse(url=portal.url)


@router.post("/webhook", response_model=WebhookResponse)
async def billing_webhook(request: Request, db: Session = Depends(get_db)):
    """Receive a provider webhook.

    The raw body is read before anything else and passed through
    untouched: the signature covers those exact bytes, and a body parser
    that re-serialised them would produce a different HMAC and reject
    every genuine event.

    A verified event is answered 200 whether or not it changed anything,
    because a provider retries on any non-2xx and a permanently failing
    endpoint stops delivering the events that do matter. A failed
    *verification* is answered 400 and never applies anything.

    There is deliberately no ``Depends(get_current_user_from_token)`` and
    no ``Depends(require_api_key(...))`` here. The provider holds no
    session, and a second gate in front of the signature gate is a gate
    that can be mis-configured into accepting unsigned payloads.
    """
    payload = await request.body()
    signature = request.headers.get("Stripe-Signature")

    try:
        event = _provider().handle_webhook(payload=payload, signature=signature)
    except WebhookSignatureError as exc:
        logger.warning("Rejected billing webhook: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "invalid_signature",
                "message": "Webhook signature verification failed",
            },
        ) from exc

    try:
        applied = EntitlementService(db).apply_billing_event(event)
    except Exception:
        # A database failure is the provider's problem to retry, not
        # something to answer 2xx for: an acknowledged-but-unapplied
        # event is a lost plan change.
        logger.exception("Billing webhook application failed for %s", event.event_id)
        raise

    return WebhookResponse(
        received=True,
        event_id=applied.event_id,
        event_type=applied.event_type,
        duplicate=applied.duplicate,
        applied=applied.applied,
        account_id=applied.account_id,
        plan=applied.plan,
    )
