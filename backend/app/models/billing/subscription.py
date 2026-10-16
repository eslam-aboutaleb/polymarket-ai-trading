"""Subscription model — the plan tier an account holds (plan 09 Phase A).

Ownership follows master plan 5.1: the tier lives on the **account**, not on
the user. A sub-account therefore inherits the owner's plan through its own
row and can be moved independently later without a schema change, while
``user_settings`` stays 1:1 with ``User`` and untouched.

The row is the *only* record of the plan. There is deliberately no plan
column on ``users`` or ``user_settings`` to keep in sync: a second writer of
the same fact is how a paywall leaks, so :mod:`app.services.users.entitlement_service`
is the single place that reads this table and the single place that writes it.

Invariants:

* One row per account. A plan change rewrites the row rather than appending,
  so ``provider_subscription_id`` stays stable across upgrades and the
  "current plan" question is a single indexed lookup.
* ``plan`` is an enum, never a free-text tier name, so an unknown value
  cannot silently widen entitlements.
* ``current_period_end`` / ``cancel_at_period_end`` describe the provider's
  billing period, not the entitlement window: a downgrade takes effect at
  the period boundary, and until then the account keeps what it paid for.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)

from app.models.base import Base
from app.utils.time import utc_now


class PlanTier(StrEnum):
    """The three v1 plan tiers.

    Ordered cheapest-first so ``PLAN_ORDER`` can answer "which plan does
    this capability need" without a second table. A value that is not a
    member of this enum is never a valid plan: unknown strings coerce to
    ``free`` (fail closed) rather than being trusted.
    """

    FREE = "free"
    PRO = "pro"
    INSTITUTIONAL = "institutional"

    @classmethod
    def coerce(cls, value: object) -> PlanTier:
        """Map anything to a plan tier, defaulting to ``free``."""
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for tier in cls:
            if tier.value == text:
                return tier
        return cls.FREE


class SubscriptionStatus(StrEnum):
    """Lifecycle of one subscription row.

    ``none`` is the provider-less state of a backfilled account: it exists,
    it is free, and no billing provider has ever heard of it.
    """

    NONE = "none"
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"

    @classmethod
    def coerce(cls, value: object) -> SubscriptionStatus:
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for status in cls:
            if status.value == text:
                return status
        return cls.NONE


class BillingProviderKind(StrEnum):
    """Which payment provider owns the row.

    ``none`` is the default and the only value a backfilled account gets:
    entitlements work before any billing is configured, which is what lets
    the free tier ship with Stripe credentials absent.
    """

    NONE = "none"
    STRIPE = "stripe"

    @classmethod
    def coerce(cls, value: object) -> BillingProviderKind:
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for kind in cls:
            if kind.value == text:
                return kind
        return cls.NONE


class Subscription(Base):
    """The plan one account is entitled to."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "plan IN ('free', 'pro', 'institutional')",
            name="ck_subscriptions_plan",
        ),
        CheckConstraint(
            "status IN ('none', 'active', 'trialing', 'past_due', 'canceled', 'incomplete')",
            name="ck_subscriptions_status",
        ),
        CheckConstraint(
            "provider IN ('none', 'stripe')",
            name="ck_subscriptions_provider",
        ),
        # Unique so a webhook naming a provider subscription resolves to
        # exactly one row; NULLs are exempt in both SQLite and Postgres,
        # so any number of provider-less free accounts may coexist.
        Index(
            "ix_subscriptions_provider_subscription_id",
            "provider_subscription_id",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    # The account is the owner of the entitlement, not the user: a
    # sub-account resolves its own tier through its own row, and the owner
    # is always reachable through ``accounts.owner_user_id``. There is
    # deliberately no ``user_id`` column — the tier is a property of the
    # account per master plan 5.1, and a second copy of the owner here is
    # a second thing to keep in sync.
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)

    plan = Column(
        String(20),
        nullable=False,
        default=PlanTier.FREE.value,
        server_default=PlanTier.FREE.value,
    )
    status = Column(
        String(20),
        nullable=False,
        default=SubscriptionStatus.NONE.value,
        server_default=SubscriptionStatus.NONE.value,
    )
    provider = Column(
        String(20),
        nullable=False,
        default=BillingProviderKind.NONE.value,
        server_default=BillingProviderKind.NONE.value,
    )

    # Provider handles. Nullable for every free account, and unique so a
    # webhook for a known subscription resolves to exactly one row.
    provider_customer_id = Column(String(120), nullable=True)
    provider_subscription_id = Column(String(120), nullable=True)

    current_period_end = Column(DateTime(timezone=True), nullable=True)
    # A cancelled subscription that runs to the period boundary keeps its
    # entitlements until then; this flag is what the UI shows.
    cancel_at_period_end = Column(Boolean, default=False, nullable=False, server_default="0")

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<Subscription(account_id={self.account_id}, plan={self.plan}, "
            f"status={self.status}, provider={self.provider})>"
        )


class BillingWebhookEvent(Base):
    """Ledger of applied provider webhooks — the idempotency key.

    Lives beside :class:`Subscription` rather than in its own module
    because it has no independent meaning: it exists only to answer "have
    I already applied this event?". The unique ``event_id`` is the whole
    mechanism — a replayed delivery finds its row and changes nothing,
    which is what makes the webhook safe to accept from a provider that
    retries on any non-2xx.

    Rows are never deleted. The table doubles as the audit trail for "who
    changed this account's plan and when", which is the question a
    disputed charge always turns into.
    """

    __tablename__ = "billing_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    # The provider's own event id (``evt_...`` for Stripe). Unique and
    # non-null: it is the idempotency key, and a row without one could
    # never be matched on a replay.
    event_id = Column(String(120), nullable=False, unique=True, index=True)
    event_type = Column(String(100), nullable=False, server_default="")
    provider = Column(
        String(20),
        nullable=False,
        default=BillingProviderKind.STRIPE.value,
        server_default=BillingProviderKind.STRIPE.value,
    )
    # Nullable: an event that names no account (a deleted customer, a
    # subscription we never saw) is still recorded, so a retry after the
    # account appears can be correlated against it.
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True, index=True)
    # The plan the event applied, or NULL when it applied nothing.
    plan = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return f"<BillingWebhookEvent(event_id={self.event_id}, type={self.event_type})>"
