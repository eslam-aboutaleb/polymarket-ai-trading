"""Subscription and webhook-ledger persistence queries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing.subscription import BillingWebhookEvent, Subscription


class SubscriptionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_for_account(self, account_id: int) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(Subscription.account_id == account_id)
            .order_by(Subscription.id.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def get_by_account_id(self, account_id: int) -> Subscription | None:
        return self.get_for_account(account_id)

    def get_by_provider_subscription_id(self, subscription_id: str) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(Subscription.provider_subscription_id == subscription_id)
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def get_by_provider_sub_id(self, subscription_id: str) -> Subscription | None:
        return self.get_by_provider_subscription_id(subscription_id)

    def get_by_provider_customer_id(self, customer_id: str) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(Subscription.provider_customer_id == customer_id)
            .order_by(Subscription.id.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def get_webhook_event(self, event_id: str) -> BillingWebhookEvent | None:
        stmt = select(BillingWebhookEvent).where(BillingWebhookEvent.event_id == event_id).limit(1)
        return self.db.execute(stmt).scalars().first()

    def set_provider_customer_id(
        self,
        subscription: Subscription,
        customer_id: str,
        *,
        provider: str,
    ) -> Subscription:
        """Persist the provider customer identity at the checkout boundary."""
        subscription.provider_customer_id = customer_id
        subscription.provider = provider
        self.db.commit()
        return subscription
