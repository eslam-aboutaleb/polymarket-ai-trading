"""Entitlement service — the single source of truth for what a plan allows.

Every plan question in the system is answered here. Routes call
``require_plan(capability)`` (a thin FastAPI dependency in
:mod:`app.api.deps`), background jobs call ``EntitlementService.can(...)``,
and the quota counters call ``consume_quota(...)``. There is deliberately
**no** second place that inspects a plan: ``if account.plan == "pro"`` in a
route is a bug, because the next capability added to the matrix would
silently miss it.

Invariants
----------

* **Fail closed.** An unknown capability, an unknown plan string or a
  missing row resolves to the free tier. A mistake can only *lose* an
  entitlement, never grant one.
* **Additive gating only.** A downgrade revokes the right to *start* new
  automated activity and strips external trade scopes. It never deletes
  rows, cancels orders, liquidates positions or sets a trading halt — see
  :meth:`EntitlementService.apply_plan` and
  :meth:`EntitlementService.disable_lost_automation`, both covered by
  ``tests/test_entitlement.py``.
* **Money path untouched.** Nothing here raises a risk cap or loosens
  ``pre_trade_gate.apply_global_safety_caps``; a gate can only refuse an
  action the plan does not pay for.
* **Refusals are domain errors.** The gates raise ``EntitlementDenied``
  / ``QuotaExceeded`` (``app.services.errors``), never a FastAPI
  ``HTTPException``, so a scheduler or a CLI hits the same wall as a
  route; the API layer is what turns them into a 402/429
  (``app.api.deps._http_error``).
* **Single writer.** ``apply_plan`` and ``apply_billing_event`` are the
  only functions that write the plan column, so a provider webhook and a
  route cannot disagree.
* **Enforcement is roll-out gated** (``entitlements_enabled``, default
  false) per master plan I12, so the matrix can be deployed and reviewed
  before any request is refused. The dependency is always wired; the
  switch decides whether it bites.
* **Quotas are soft.** The counter lives in the cache with a daily TTL. A
  cache miss or an eviction widens one bucket's budget; a quota is a cost
  control, never a safety mechanism, and a cache failure is logged and
  allowed through rather than failing the request.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.billing.subscription import (
    BillingProviderKind,
    BillingWebhookEvent,
    PlanTier,
    Subscription,
    SubscriptionStatus,
)
from app.models.users.account import Account
from app.repositories.billing.subscription_repository import SubscriptionRepository
from app.repositories.trading.entitlement_repository import TradingEntitlementRepository
from app.repositories.users.account_repository import AccountRepository
from app.repositories.users.api_key_repository import ApiKeyRepository
from app.services.errors import (
    AccountLimitExceeded,
    CapabilityUnknown,
    EntitlementDenied,
    QuotaExceeded,
)
from app.services.users.api_key_service import SCOPE_READ, SCOPE_TRADE
from app.utils.cache import get_cache
from app.utils.time import utc_now

logger = logging.getLogger(__name__)


# ── Capability vocabulary ───────────────────────────────────────
# One constant per gated surface. A route never names a plan, only a
# capability, so adding "whale streaming to pro" is a one-line matrix edit
# rather than a hunt through the route modules.

#: Manual order placement. Always available, including on free, and never
#: behind an email gate (master plan risk note: gating too much kills the
#: product).
CAP_MANUAL_TRADING = "manual_trading"

#: Market browse / search / prices.
CAP_MARKET_BROWSING = "market_browsing"

#: The single-shot analysis surface (market, quick, scan, trade-plan) that
#: does not fan out into a multi-step plan.
CAP_BASIC_ANALYSIS = "basic_analysis"

#: Local backtest runs: no LLM cost, no exchange writes.
CAP_BACKTESTING = "backtesting"

#: Multi-step AI orchestration: the most expensive surface, a plan of
#: subtasks per request.
CAP_ORCHESTRATION = "orchestration"

#: The opportunity / easy-trade scan: one ranked sweep across markets.
CAP_OPPORTUNITY_SCAN = "opportunity_scan"

#: On-demand AI news generation.
CAP_NEWS_GENERATION = "news_generation"

CAP_AI_ANALYSIS = "ai_analysis"
CAP_ALERTS = "alerts"

CAP_COPY_TRADING = "copy_trading"
CAP_MARKET_MAKER = "market_maker"
CAP_INVERSE_BOT = "inverse_bot"
CAP_LATENCY_ARB = "latency_arb"

CAP_SUB_ACCOUNTS = "sub_accounts"

#: External API read (``GET /api/v1/external/*``).
CAP_EXTERNAL_API_READ = "external_api_read"

#: External API write (``POST /api/v1/external/orders``). Kept distinct
#: from the read capability because institutional is the only tier that
#: gets it.
CAP_EXTERNAL_API_TRADE = "external_api_trade"

#: Compliance / audit export.
CAP_COMPLIANCE_EXPORT = "compliance_export"

CAP_PRIORITY_SUPPORT = "priority_support"

#: Surfaces that consume a per-plan daily budget. Everything else is a
#: boolean capability.
QUOTA_SURFACES: tuple[str, ...] = (CAP_ORCHESTRATION, CAP_OPPORTUNITY_SCAN, CAP_NEWS_GENERATION)


# ── The matrix ──────────────────────────────────────────────────

_FREE_CAPABILITIES = frozenset(
    {
        CAP_MANUAL_TRADING,
        CAP_MARKET_BROWSING,
        CAP_BASIC_ANALYSIS,
        CAP_BACKTESTING,
    }
)

_PRO_CAPABILITIES = frozenset(
    {
        CAP_AI_ANALYSIS,
        CAP_ALERTS,
        CAP_COPY_TRADING,
        CAP_MARKET_MAKER,
        CAP_INVERSE_BOT,
        CAP_LATENCY_ARB,
        CAP_SUB_ACCOUNTS,
        CAP_EXTERNAL_API_READ,
        CAP_ORCHESTRATION,
        CAP_OPPORTUNITY_SCAN,
        CAP_NEWS_GENERATION,
    }
)

_INSTITUTIONAL_CAPABILITIES = frozenset(
    {
        CAP_EXTERNAL_API_TRADE,
        CAP_COMPLIANCE_EXPORT,
        CAP_PRIORITY_SUPPORT,
    }
)

#: Capability → plans that hold it. Built by union so a capability can
#: never appear in ``institutional`` without also existing in ``pro``.
PLAN_CAPABILITIES: dict[str, frozenset[str]] = {
    PlanTier.FREE.value: _FREE_CAPABILITIES,
    PlanTier.PRO.value: _FREE_CAPABILITIES | _PRO_CAPABILITIES,
    PlanTier.INSTITUTIONAL.value: _FREE_CAPABILITIES
    | _PRO_CAPABILITIES
    | _INSTITUTIONAL_CAPABILITIES,
}

#: Cheapest plan first.
PLAN_ORDER: tuple[str, ...] = (
    PlanTier.FREE.value,
    PlanTier.PRO.value,
    PlanTier.INSTITUTIONAL.value,
)

#: Every capability any plan grants. ``ALL_CAPABILITIES`` is what makes an
#: unknown capability a 403 rather than a silently-free one.
ALL_CAPABILITIES: frozenset[str] = frozenset().union(*PLAN_CAPABILITIES.values())

#: Human-readable labels, reused verbatim by the frontend through
#: ``GET /api/billing/plans``.
PLAN_LABELS: dict[str, str] = {
    PlanTier.FREE.value: "Free",
    PlanTier.PRO.value: "Pro",
    PlanTier.INSTITUTIONAL.value: "Institutional",
}

#: Statuses whose row's ``plan`` column governs entitlements. ``NONE`` is
#: the provider-less state of a backfilled account, so a locally granted
#: plan (admin override, test fixture) works with no billing configured.
_PLAN_BEARING_STATUSES = frozenset(
    {
        SubscriptionStatus.NONE,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.PAST_DUE,
    }
)

#: Statuses that mean "not entitled": the provider never completed the
#: subscription, or it was cancelled outright.
_UNENTITLED_STATUSES = frozenset(
    {
        SubscriptionStatus.CANCELED,
        SubscriptionStatus.INCOMPLETE,
    }
)


# ── Limits ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanLimits:
    """Numeric ceilings for one plan.

    ``None`` means unlimited; ``0`` means "not on this plan" (the boolean
    capability gate refuses first, so a zero budget is a belt-and-braces
    default rather than the primary control).
    """

    max_accounts: int | None = 1
    max_followed_traders: int | None = 0
    quotas: dict[str, int | None] = field(default_factory=dict)

    def quota_for(self, capability: str) -> int | None:
        """Daily budget for ``capability``; ``0`` when the plan has none."""
        return self.quotas.get(capability, 0)


_FREE_QUOTAS = dict.fromkeys(QUOTA_SURFACES, 0)
_PRO_QUOTAS = {
    CAP_ORCHESTRATION: 50,
    CAP_OPPORTUNITY_SCAN: 100,
    CAP_NEWS_GENERATION: 50,
}
_INSTITUTIONAL_QUOTAS = dict.fromkeys(QUOTA_SURFACES)


def plan_limits(plan: object) -> PlanLimits:
    """Return the limits for ``plan``, coercing an unknown value to free."""
    tier = PlanTier.coerce(plan)
    if tier is PlanTier.PRO:
        return PlanLimits(max_accounts=5, max_followed_traders=50, quotas=dict(_PRO_QUOTAS))
    if tier is PlanTier.INSTITUTIONAL:
        return PlanLimits(
            max_accounts=None,
            max_followed_traders=None,
            quotas=dict(_INSTITUTIONAL_QUOTAS),
        )
    return PlanLimits(max_accounts=1, max_followed_traders=0, quotas=dict(_FREE_QUOTAS))


def capabilities_for(plan: object) -> frozenset[str]:
    """The capability set of ``plan``, coercing an unknown value to free."""
    return PLAN_CAPABILITIES[PlanTier.coerce(plan).value]


def required_plan_for(capability: str) -> str | None:
    """Cheapest plan that grants ``capability``, or ``None`` if none does."""
    if capability not in ALL_CAPABILITIES:
        return None
    for plan in PLAN_ORDER:
        if capability in PLAN_CAPABILITIES[plan]:
            return plan
    return None


def _rank(plan: str) -> int:
    """Position of ``plan`` on the cheapest-first ladder."""
    try:
        return PLAN_ORDER.index(PlanTier.coerce(plan).value)
    except ValueError:  # pragma: no cover - PLAN_ORDER covers every member
        return 0


#: Settings flag → the capability needed to switch it **on**. Turning one
#: off is always allowed: a downgrade must never be blocked from stopping
#: something, and a user who turns a strategy off by hand is exercising
#: the same additive gating the plan does.
SETTINGS_TOGGLE_CAPABILITIES: dict[str, str] = {
    "copy_trading_enabled": CAP_COPY_TRADING,
    "inverse_bot_enabled": CAP_INVERSE_BOT,
}


#: Email shape check. Deliberately conservative and dependency-free: this
#: is an "is this plausibly a mailbox" gate for a Stripe customer, not an
#: RFC 5322 parser, and ``email-validator`` is not a backend dependency.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
MAX_EMAIL_LENGTH = 254


def is_valid_email(value: object) -> bool:
    """Whether ``value`` is a plausible email address."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_EMAIL_LENGTH:
        return False
    return bool(_EMAIL_RE.match(text))


# ── Errors ──────────────────────────────────────────────────────


def _paywall(capability: str, current_plan: str, required_plan: str) -> EntitlementDenied:
    """The 402 every gated surface raises.

    The body is structured so the frontend can render an upgrade CTA from
    it without a second lookup, and it carries a plain ``message`` so the
    shared ``getApiErrorMessage`` helper shows something readable.

    Only the plan labels are resolved here: ``PLAN_LABELS`` is this
    module's data, while the body shape belongs to
    :class:`~app.services.errors.EntitlementDenied` so a non-HTTP caller
    sees the same structured refusal a route does.
    """
    return EntitlementDenied(
        capability=capability,
        current_plan=current_plan,
        required_plan=required_plan,
        message=(
            f"Your {PLAN_LABELS.get(current_plan, current_plan)} plan does not include "
            f"{capability.replace('_', ' ')}. Upgrade to "
            f"{PLAN_LABELS.get(required_plan, required_plan)} to enable it."
        ),
    )


def _quota_exceeded(capability: str, limit: int, retry_after_seconds: int) -> QuotaExceeded:
    """The 429 a spent daily budget raises."""
    return QuotaExceeded(
        capability=capability,
        limit=limit,
        retry_after_seconds=retry_after_seconds,
    )


# ── Webhook normalisation ───────────────────────────────────────


@dataclass(frozen=True)
class BillingEvent:
    """A provider webhook, normalised.

    The provider is responsible for signature verification and for turning
    its payload into this shape;
    :class:`EntitlementService` is responsible for applying it. That split
    is what keeps the plan column single-writer: a second provider only
    has to produce this dataclass, never a database write.
    """

    event_id: str
    event_type: str
    provider: str = BillingProviderKind.STRIPE.value
    plan: str | None = None
    account_id: int | None = None
    provider_customer_id: str | None = None
    provider_subscription_id: str | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    status: str = SubscriptionStatus.ACTIVE.value


@dataclass(frozen=True)
class AppliedEvent:
    """Outcome of applying a :class:`BillingEvent`."""

    event_id: str
    event_type: str
    duplicate: bool
    applied: bool
    account_id: int | None = None
    plan: str | None = None


def _next_utc_midnight(moment: datetime) -> datetime:
    return datetime.combine(moment.date() + timedelta(days=1), time(), tzinfo=UTC)


class EntitlementService:
    """Answers plan questions and is the only writer of the plan column."""

    def __init__(self, db: Session):
        self.db = db

    # ── Rollout switch ──

    def enforcement_enabled(self) -> bool:
        """Whether gates actually refuse requests (master plan I12)."""
        return bool(getattr(get_settings(), "entitlements_enabled", False))

    # ── Reading the plan ──

    def subscription_for(self, account: Account | None) -> Subscription:
        """Return the account's subscription row, creating a free one if absent.

        Provisioning is lazy, exactly like ``ensure_personal_account``: a
        database that predates the migration backfill, or an account
        created by a concurrent request, still resolves. The row is
        created committed, because a caller that leaves an uncommitted
        free row behind would roll the account back to "no plan" on its
        next error path.
        """
        if account is None:
            raise ValueError("an account is required to resolve a subscription")

        row = SubscriptionRepository(self.db).get_for_account(account.id)
        if row is not None:
            return row

        row = Subscription(
            account_id=account.id,
            plan=PlanTier.FREE.value,
            status=SubscriptionStatus.NONE.value,
            provider=BillingProviderKind.NONE.value,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        logger.info(
            "Provisioned free subscription for account_id=%s",
            account.id,
        )
        return row

    def attach_provider_customer(
        self, subscription: Subscription, customer_id: str
    ) -> Subscription:
        """Persist the Stripe customer reference before checkout continues."""
        return SubscriptionRepository(self.db).set_provider_customer_id(
            subscription,
            customer_id,
            provider=BillingProviderKind.STRIPE.value,
        )

    def effective_plan(self, account: Account | None) -> str:
        """The plan that governs entitlements right now.

        A cancelled or never-completed subscription is free. ``past_due``
        keeps its plan on purpose: a failed card must not stop a running
        strategy mid-flight, and the provider cancels the row itself when
        the retry window closes.
        """
        if account is None:
            return PlanTier.FREE.value
        row = self.subscription_for(account)
        if SubscriptionStatus.coerce(row.status) in _UNENTITLED_STATUSES:
            return PlanTier.FREE.value
        return PlanTier.coerce(row.plan).value

    def plan_for(self, account: Account | None) -> str:
        """Alias of :meth:`effective_plan` for readability at call sites."""
        return self.effective_plan(account)

    def status_for(self, account: Account | None) -> str:
        if account is None:
            return SubscriptionStatus.NONE.value
        return SubscriptionStatus.coerce(self.subscription_for(account).status).value

    # ── Capability checks ──

    def can(self, account: Account | None, capability: str) -> bool:
        """Whether ``account`` holds ``capability``.

        Never raises and never consults the rollout switch: this is the
        predicate routes, schedulers and tests read, and it must answer
        the same question whether or not enforcement is switched on.
        """
        return capability in capabilities_for(self.effective_plan(account))

    def plan_limits(self, account: Account | None) -> PlanLimits:
        """The limits of the account's effective plan."""
        return plan_limits(self.effective_plan(account))

    def limits_for(self, account: Account | None) -> PlanLimits:
        """Alias of :meth:`plan_limits`."""
        return self.plan_limits(account)

    def assert_capability(self, account: Account | None, capability: str) -> None:
        """Refuse ``capability`` unless the plan grants it.

        Raises :class:`~app.services.errors.EntitlementDenied` (402) or
        :class:`~app.services.errors.CapabilityUnknown` (403); the API
        layer translates both. A no-op while ``entitlements_enabled`` is
        false, which is what keeps the rollout additive: the dependency
        is wired on every gated route and the switch decides whether it
        bites.
        """
        if not self.enforcement_enabled():
            return
        plan = self.effective_plan(account)
        if capability in capabilities_for(plan):
            return

        required = required_plan_for(capability)
        if required is None:
            # Not a capability any plan grants: refuse without hinting at
            # an upgrade that would not help.
            logger.warning(
                "Refused unknown capability %r for account_id=%s",
                capability,
                getattr(account, "id", None),
            )
            raise CapabilityUnknown(capability)
        logger.info(
            "Plan gate refused capability=%s account_id=%s plan=%s required=%s",
            capability,
            getattr(account, "id", None),
            plan,
            required,
        )
        raise _paywall(capability, plan, required)

    def assert_account_limit(self, account: Account | None, current_count: int) -> None:
        """Refuse creating one more account beyond the plan's ceiling.

        ``current_count`` is the number of accounts the owner already
        holds, so the check works for both the API route and a job
        without either one re-deriving the limit.
        """
        if not self.enforcement_enabled():
            return
        plan = self.effective_plan(account)
        ceiling = plan_limits(plan).max_accounts
        if ceiling is None or current_count < ceiling:
            return
        logger.info(
            "Account limit reached: account_id=%s plan=%s count=%s ceiling=%s",
            getattr(account, "id", None),
            plan,
            current_count,
            ceiling,
        )
        raise AccountLimitExceeded(
            current_plan=plan,
            limit=ceiling,
            used=current_count,
            message=(
                f"Your {PLAN_LABELS.get(plan, plan)} plan allows {ceiling} "
                "account(s). Upgrade to add more."
            ),
        )

    def assert_account_creation_allowed(self, owner_user_id: int) -> None:
        """Refuse creating another account for ``owner_user_id``.

        The ceiling is per owner, and the plan that applies is the owner's
        personal one — a sub-account inherits the tier its owner pays for,
        so counting against the caller's personal row is the only rule that
        keeps "5 accounts" from meaning something different per account.

        Reads only: an owner with no account at all is treated as free
        rather than having one provisioned as a side effect of a limit
        check.
        """
        if not self.enforcement_enabled():
            return
        accounts = AccountRepository(self.db).list_for_owner_by_id(owner_user_id)
        subject = accounts[0] if accounts else None
        self.assert_account_limit(subject, len(accounts))

    def assert_settings_toggles(self, account: Account | None, updates: dict) -> None:
        """Refuse a settings write that switches a paid strategy on.

        The generic settings PATCH is a single route covering every field,
        so the gate lives here rather than in the route: a field that turns
        automation on is checked against the matrix, and everything else
        (risk limits, paper balance, provider choice) is never gated —
        refusing those would be a paywall on the user's own risk posture.

        Disabling is never refused, which is what lets a downgraded account
        tidy up after itself.
        """
        if not self.enforcement_enabled():
            return
        for field_name, capability in SETTINGS_TOGGLE_CAPABILITIES.items():
            if updates.get(field_name) is True:
                self.assert_capability(account, capability)

    # ── Quotas ──
    @staticmethod
    def _bucket(moment: datetime) -> str:
        """The daily quota bucket: one UTC day."""
        return moment.date().isoformat()

    @staticmethod
    def _seconds_until_midnight(moment: datetime) -> int:
        return max(1, int((_next_utc_midnight(moment) - moment).total_seconds()))

    def _counter_key(self, account: Account | None, capability: str, bucket: str) -> str:
        identity = f"account:{getattr(account, 'id', 'anonymous')}"
        return f"quota:{capability}:{identity}:{bucket}"

    @staticmethod
    def _cache():
        """The shared quota cache namespace.

        In-memory outside Redis, which is the documented soft-limit
        trade-off: with more than one worker the budget is per process.
        """
        return quota_cache

    def quota_usage(self, account: Account | None, capability: str) -> tuple[int, int | None]:
        """Return ``(used, limit)`` for one surface without consuming."""
        limit = self.plan_limits(account).quota_for(capability)
        if limit is None:
            return 0, None
        cache = self._cache()
        raw = cache.get(self._counter_key(account, capability, self._bucket(utc_now())))
        try:
            return int(raw or 0), limit
        except (TypeError, ValueError):
            return 0, limit

    def consume_quota(self, account: Account | None, capability: str, cost: int = 1) -> int:
        """Spend ``cost`` against the daily budget, refusing when spent.

        Raises :class:`~app.services.errors.QuotaExceeded` (429) when the
        budget is gone, and the paywall (402) or
        :class:`~app.services.errors.CapabilityUnknown` (403) when the
        plan never had a budget for the surface at all.

        The counter is a read-modify-write in the shared cache, so two
        concurrent requests can both observe the pre-increment value.
        That is accepted: a quota is a cost control on an expensive LLM
        surface, never a safety mechanism, and the plan's own risk note
        asks for the soft behaviour.
        """
        if not self.enforcement_enabled():
            return 0
        limit = self.plan_limits(account).quota_for(capability)
        if limit is None:
            return 0
        now = utc_now()
        if limit <= 0:
            # The capability gate should already have refused this; refuse
            # again with the paywall rather than a 429, because "not on
            # your plan" and "you have used today's allowance" are
            # different answers and the second one would send the user
            # looking for a reset button that does not exist.
            required = required_plan_for(capability)
            plan_name = self.effective_plan(account)
            if required is None:
                raise CapabilityUnknown(
                    capability,
                    f"'{capability}' has no quota on any plan.",
                )
            raise _paywall(capability, plan_name, required)

        cache = self._cache()
        key = self._counter_key(account, capability, self._bucket(now))
        raw = cache.get(key)
        try:
            used = int(raw or 0)
        except (TypeError, ValueError):
            logger.warning("Unreadable quota counter %s; restarting the bucket", key)
            used = 0

        if used + cost > limit:
            logger.info(
                "Quota exhausted: capability=%s account_id=%s used=%s limit=%s",
                capability,
                getattr(account, "id", None),
                used,
                limit,
            )
            raise _quota_exceeded(capability, limit, self._seconds_until_midnight(now))

        cache.set(key, used + cost, ttl_seconds=self._seconds_until_midnight(now))
        return used + cost

    def usage_report(self, account: Account | None) -> dict:
        """Usage versus quota for the billing UI.

        Accounts are counted from the owner's rows rather than from the
        plan, so the bar moves the moment a sub-account is created.
        """
        from app.services.users.account_service import list_accounts

        plan = self.effective_plan(account)
        limits = plan_limits(plan)
        account_count = 0
        if account is not None:
            account_count = len(list_accounts(self.db, account.owner_user_id))

        quotas = []
        for capability in QUOTA_SURFACES:
            used, limit = self.quota_usage(account, capability)
            quotas.append(
                {
                    "capability": capability,
                    "used": used,
                    "limit": limit,
                    "unlimited": limit is None,
                }
            )

        return {
            "plan": plan,
            "plan_label": PLAN_LABELS.get(plan, plan),
            "status": self.status_for(account),
            "accounts": {
                "used": account_count,
                "limit": limits.max_accounts,
                "unlimited": limits.max_accounts is None,
            },
            "followed_traders": {
                "used": self._followed_trader_count(account),
                "limit": limits.max_followed_traders,
                "unlimited": limits.max_followed_traders is None,
            },
            "quotas": quotas,
            "capabilities": sorted(capabilities_for(plan)),
        }

    def _followed_trader_count(self, account: Account | None) -> int:
        if account is None:
            return 0
        return TradingEntitlementRepository(self.db).count_followed_traders(account.owner_user_id)

    # ── Writing the plan ──

    def apply_plan(
        self,
        account: Account,
        plan: str,
        *,
        status_: str = SubscriptionStatus.ACTIVE.value,
        provider: str = BillingProviderKind.NONE.value,
        provider_customer_id: str | None = None,
        provider_subscription_id: str | None = None,
        current_period_end: datetime | None = None,
        cancel_at_period_end: bool = False,
    ) -> Subscription:
        """Set the account's plan, applying a downgrade additively.

        This is the only writer of the plan column. On a downgrade it calls
        :meth:`disable_lost_automation`, which flips the toggles that start
        *new* automated activity and strips external trade scopes — and
        nothing else.
        """
        target = PlanTier.coerce(plan)
        previous = self.effective_plan(account)
        row = self.subscription_for(account)

        row.plan = target.value
        row.status = SubscriptionStatus.coerce(status_).value
        row.provider = BillingProviderKind.coerce(provider).value
        if provider_customer_id is not None:
            row.provider_customer_id = provider_customer_id
        if provider_subscription_id is not None:
            row.provider_subscription_id = provider_subscription_id
        row.current_period_end = current_period_end
        row.cancel_at_period_end = bool(cancel_at_period_end)
        self.db.commit()
        self.db.refresh(row)

        if _rank(target.value) < _rank(previous):
            logger.info(
                "Plan downgrade account_id=%s %s -> %s; revoking new automated activity",
                account.id,
                previous,
                target.value,
            )
            self.disable_lost_automation(account, previous, target.value)
        return row

    def disable_lost_automation(
        self,
        account: Account,
        previous_plan: str,
        new_plan: str,
    ) -> list[str]:
        """Revoke the right to *start* activity the new plan does not include.

        Additive gating, and the reason this method exists rather than a
        ``DELETE``:

        * the account's settings row, trades, positions, stop-losses and
          history are never touched;
        * a running engine is never signalled to stop — it simply finds
          the toggle off on its next cycle and starts nothing new;
        * ``trading_halted``, ``halt_reason`` and ``peak_capital`` are left
          exactly as the risk engines wrote them, so a downgrade can never
          look like a safety event;
        * existing external API keys are kept and only lose the ``trade``
          scope, so an integration keeps working read-only.

        Returns the settings fields that were flipped, for the audit log.
        """
        lost = capabilities_for(previous_plan) - capabilities_for(new_plan)
        if not lost:
            return []

        flipped: list[str] = []
        settings = self._settings_for(account)

        if CAP_COPY_TRADING in lost and getattr(settings, "copy_trading_enabled", False):
            settings.copy_trading_enabled = False
            flipped.append("copy_trading_enabled")
        if CAP_INVERSE_BOT in lost and getattr(settings, "inverse_bot_enabled", False):
            settings.inverse_bot_enabled = False
            flipped.append("inverse_bot_enabled")
        if CAP_MARKET_MAKER in lost:
            flipped.extend(self._disable_market_makers(account))
        if CAP_LATENCY_ARB in lost:
            flipped.extend(self._disable_latency_arb(account))
        if CAP_EXTERNAL_API_TRADE in lost:
            flipped.extend(self._revoke_external_trade_scope(account))

        if flipped:
            self.db.commit()
            logger.info(
                "Revoked automation for account_id=%s (%s -> %s): %s",
                account.id,
                previous_plan,
                new_plan,
                ", ".join(flipped),
            )
        return flipped

    def _settings_for(self, account: Account):
        # Local import: ``account_service`` builds settings rows and the
        # account routes import this module through ``api.deps``.
        from app.services.users.account_service import get_settings_row

        return get_settings_row(self.db, account)

    def _disable_market_makers(self, account: Account) -> list[str]:
        rows = TradingEntitlementRepository(self.db).list_enabled_market_makers(
            account.owner_user_id
        )
        for row in rows:
            row.enabled = False
        return [f"market_maker_config:{row.id}" for row in rows]

    def _disable_latency_arb(self, account: Account) -> list[str]:
        row = TradingEntitlementRepository(self.db).get_latency_arb_config(account.owner_user_id)
        if row is None or not row.enabled:
            return []
        row.enabled = False
        return ["latency_arb_config"]

    def _revoke_external_trade_scope(self, account: Account) -> list[str]:
        """Drop the ``trade`` scope from the owner's API keys, keeping the keys.

        The key row is never deleted: revoking a credential is not the same
        as removing the plan, and an integration reading data must survive
        a downgrade.
        """
        rows = ApiKeyRepository(self.db).list_unrevoked_for_user(account.owner_user_id)
        revoked: list[str] = []
        for row in rows:
            if SCOPE_TRADE not in _parse_scopes(row.scopes):
                continue
            row.scopes = _dump_scopes([SCOPE_READ])
            revoked.append(f"api_key:{row.id}")
        return revoked

    # ── Webhook application ──

    def apply_billing_event(self, event: BillingEvent) -> AppliedEvent:
        """Apply a normalised webhook, idempotent by ``event_id``.

        A replayed event returns ``duplicate=True`` and touches nothing:
        the ledger row is written in the same transaction as the plan
        change, so a crash between the two cannot leave a subscription
        upgraded without its audit row.
        """
        existing = SubscriptionRepository(self.db).get_webhook_event(event.event_id)
        if existing is not None:
            logger.info("Ignoring replayed billing event %s", event.event_id)
            return AppliedEvent(
                event_id=event.event_id,
                event_type=existing.event_type,
                duplicate=True,
                applied=False,
                account_id=existing.account_id,
                plan=existing.plan,
            )

        account = self._account_for_event(event)
        applied_plan: str | None = None
        if account is None:
            logger.warning(
                "Billing event %s (%s) matched no account; recorded as unapplied",
                event.event_id,
                event.event_type,
            )
        elif event.plan is None:
            # An event that names no plan (a failed invoice, an unmapped
            # type) changes the billing *state* only. Treating it as a
            # downgrade would be the failure mode where a typo in a price
            # id strips a paying customer of what they bought.
            self._apply_status_only(account, event)
            applied_plan = self.effective_plan(account)
        else:
            target_plan = PlanTier.coerce(event.plan).value
            self.apply_plan(
                account,
                target_plan,
                status_=event.status,
                provider=event.provider,
                provider_customer_id=event.provider_customer_id,
                provider_subscription_id=event.provider_subscription_id,
                current_period_end=event.current_period_end,
                cancel_at_period_end=event.cancel_at_period_end,
            )
            applied_plan = target_plan

        record = BillingWebhookEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            provider=event.provider,
            account_id=getattr(account, "id", None),
            plan=applied_plan,
        )
        self.db.add(record)
        self.db.commit()
        return AppliedEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            duplicate=False,
            applied=account is not None,
            account_id=getattr(account, "id", None),
            plan=applied_plan,
        )

    def _apply_status_only(self, account: Account, event: BillingEvent) -> Subscription:
        """Record billing state without changing the plan column."""
        row = self.subscription_for(account)
        row.status = SubscriptionStatus.coerce(event.status).value
        row.provider = BillingProviderKind.coerce(event.provider).value
        if event.provider_customer_id is not None:
            row.provider_customer_id = event.provider_customer_id
        if event.provider_subscription_id is not None:
            row.provider_subscription_id = event.provider_subscription_id
        if event.current_period_end is not None:
            row.current_period_end = event.current_period_end
        row.cancel_at_period_end = bool(event.cancel_at_period_end)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _account_for_event(self, event: BillingEvent) -> Account | None:
        """Resolve the account a webhook names: by id, subscription or customer."""
        if event.account_id is not None:
            account = AccountRepository(self.db).get_by_id(event.account_id)
            if account is not None:
                return account

        if event.provider_subscription_id:
            row = SubscriptionRepository(self.db).get_by_provider_subscription_id(
                event.provider_subscription_id
            )
            if row is not None:
                return AccountRepository(self.db).get_by_id(row.account_id)

        if event.provider_customer_id:
            row = SubscriptionRepository(self.db).get_by_provider_customer_id(
                event.provider_customer_id
            )
            if row is not None:
                return AccountRepository(self.db).get_by_id(row.account_id)

        return None


def _parse_scopes(raw: str | None) -> list[str]:
    """Decode a stored scope list, tolerating a malformed row."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(scope) for scope in decoded]


#: One shared counter store for every quota bucket. ``get_cache`` returns a
#: fresh instance per call — each ``InMemoryCache`` owns its own dict — so
#: memoising it at module level is what makes a counter written by one
#: request visible to the next. With ``REDIS_URL`` set the same singleton
#: is a shared one, so the budget holds across workers.
quota_cache = get_cache("entitlement_quota")


def _dump_scopes(scopes: list[str]) -> str:
    return json.dumps(sorted(set(scopes)))
