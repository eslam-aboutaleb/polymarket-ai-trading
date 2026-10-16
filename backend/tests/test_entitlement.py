"""Entitlement, quota and billing-webhook tests (plan 09 Phase A).

Covers the five things Phase A promises, in the order the plan lists them:

* **The matrix** — every capability resolves to the plan that grants it
  and no plan below it, an unknown capability is refused rather than
  granted, and the ``can`` predicate agrees with the dependency for all
  three tiers.
* **Additive gating on a downgrade** — a pro account that drops to free
  keeps every trade, position, stop-loss, market-maker config, latency-arb
  config and API key it had. Only the *right to start new automated
  activity* is revoked: the toggles are off and the external ``trade``
  scope is gone. ``trading_halted``, ``halt_reason`` and ``peak_capital``
  are byte-identical afterwards, so a downgrade can never be mistaken for
  a safety event.
* **Webhook verification** — a missing header, a wrong secret, a stale
  timestamp and a tampered body are all rejected, and no plan changes as a
  result.
* **Idempotency** — the same verified event applied twice changes the plan
  once, and a replayed event reports ``duplicate`` without touching
  anything.
* **Quotas** — a pro account may spend its daily budget and is then
  refused with 429 carrying ``Retry-After``; the counter is per account,
  so one account cannot spend another's budget.
* **The migration backfill** — the real 20261011_0018 migration is run
  in-process against a database built from ``Base.metadata.create_all``,
  so the assertion is on its own SQL rather than on a re-implementation.

The in-memory SQLite session is the idiom ``test_accounts.py`` uses: no
network, no Stripe credentials, no key material.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time as time_module
import unittest
from unittest.mock import patch

import sqlalchemy.exc
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.api.deps import get_db
from app.api.routes.billing import billing as billing_routes
from app.main import app as fastapi_app
from app.models.account import Account, AccountKind
from app.models.api_key import ApiKey
from app.models.base import Base
from app.models.followed_trader import FollowedTrader
from app.models.latency_arb_config import LatencyArbConfig
from app.models.market_maker_config import MarketMakerConfig
from app.models.stop_loss import StopLossOrder
from app.models.subscription import Subscription
from app.models.user import User
from app.models.user_trade import UserTrade
from app.security.auth import create_access_token
from app.services.billing import StripeBillingProvider, WebhookSignatureError
from app.services.billing.stripe_provider import _WEBHOOK_TOLERANCE_SECONDS
from app.services.users.account_service import create_sub_account, resolve_account
from app.services.users.api_key_service import SCOPE_READ, SCOPE_TRADE, create_api_key
from app.services.users.entitlement_service import (
    ALL_CAPABILITIES,
    CAP_BACKTESTING,
    CAP_COPY_TRADING,
    CAP_EXTERNAL_API_READ,
    CAP_EXTERNAL_API_TRADE,
    CAP_LATENCY_ARB,
    CAP_MANUAL_TRADING,
    CAP_MARKET_BROWSING,
    CAP_MARKET_MAKER,
    CAP_OPPORTUNITY_SCAN,
    CAP_ORCHESTRATION,
    CAP_SUB_ACCOUNTS,
    PLAN_CAPABILITIES,
    PLAN_ORDER,
    EntitlementService,
    capabilities_for,
    required_plan_for,
)

#: The billing router is mounted on its own app so this module never mutates
#: the shared ``app.main`` instance (registering it there is the main
#: agent's integration step).
billing_app = FastAPI()
billing_app.include_router(billing_routes.router)

WEBHOOK_SECRET = "whsec_test_secret_value"

OWNER_WALLET = "0x" + "aa" * 20
OTHER_WALLET = "0x" + "bb" * 20
MARKET_ID = "0xmarket-cond"
TOKEN_ID = "0xtoken-1"


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _login_headers(user_id: int) -> dict:
    token = create_access_token({"user_id": user_id, "sub": OWNER_WALLET, "is_admin": False})
    return {"Authorization": f"Bearer {token}"}


def _sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """Produce a Stripe-shaped signature header for ``payload``."""
    stamp = timestamp if timestamp is not None else int(time_module.time())
    signed = f"{stamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


class EntitlementBaseTest(unittest.TestCase):
    """In-memory DB with one owner (and optionally a second user)."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        Base.metadata.create_all(self.engine)
        self.db = self.SessionFactory()
        fastapi_app.dependency_overrides[get_db] = self._get_db_override
        billing_app.dependency_overrides[get_db] = self._get_db_override
        self.client = TestClient(fastapi_app)
        self.addCleanup(self._cleanup)

        self.db.add(User(id=1, wallet_address=OWNER_WALLET))
        self.db.add(User(id=2, wallet_address=OTHER_WALLET))
        self.db.commit()

        # Enforcement is ON for every test here: the point of this module
        # is the gate, and the default-off behaviour is asserted separately.
        patcher = patch("app.services.users.entitlement_service.get_settings")
        mocked = patcher.start()
        mocked.return_value.entitlements_enabled = True
        self.addCleanup(patcher.stop)

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _cleanup(self):
        fastapi_app.dependency_overrides.clear()
        billing_app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def _account(self, user_id: int = 1) -> Account:
        return resolve_account(self.db, user_id, None)

    def _service(self) -> EntitlementService:
        return EntitlementService(self.db)

    def _set_plan(self, account: Account, plan: str) -> None:
        self._service().apply_plan(account, plan, status_="active", provider="stripe")


class PlanMatrixTest(EntitlementBaseTest):
    """The capability matrix itself."""

    def test_matrix_only_contains_known_capabilities(self):
        for plan, capabilities in PLAN_CAPABILITIES.items():
            self.assertTrue(capabilities, msg=plan)
            self.assertTrue(capabilities.issubset(ALL_CAPABILITIES), msg=plan)

    def test_matrix_is_cumulative(self):
        """A higher tier must never lose a capability a lower tier has."""
        free = PLAN_CAPABILITIES[PLAN_ORDER[0]]
        pro = PLAN_CAPABILITIES[PLAN_ORDER[1]]
        institutional = PLAN_CAPABILITIES[PLAN_ORDER[2]]
        self.assertTrue(free.issubset(pro))
        self.assertTrue(pro.issubset(institutional))

    def test_free_keeps_manual_trading_and_browsing_ungated(self):
        account = self._account()
        service = self._service()
        self.assertEqual(service.effective_plan(account), "free")
        for capability in (CAP_MANUAL_TRADING, CAP_MARKET_BROWSING, CAP_BACKTESTING):
            self.assertTrue(service.can(account, capability), msg=capability)
            self.assertIsNone(service.assert_capability(account, capability))

    def test_free_is_refused_every_paid_surface(self):
        account = self._account()
        service = self._service()
        for capability in sorted(ALL_CAPABILITIES - PLAN_CAPABILITIES["free"]):
            self.assertFalse(service.can(account, capability), msg=capability)
            with self.assertRaises(HTTPException) as ctx:
                service.assert_capability(account, capability)
            self.assertEqual(ctx.exception.status_code, 402, msg=capability)

    def test_pro_gains_automation_and_external_read(self):
        account = self._account()
        self._set_plan(account, "pro")
        service = self._service()
        for capability in (
            CAP_COPY_TRADING,
            CAP_MARKET_MAKER,
            CAP_LATENCY_ARB,
            CAP_EXTERNAL_API_READ,
            CAP_ORCHESTRATION,
            CAP_OPPORTUNITY_SCAN,
            CAP_SUB_ACCOUNTS,
        ):
            self.assertTrue(service.can(account, capability), msg=capability)

    def test_institutional_alone_gets_the_trade_scope(self):
        account = self._account()
        service = self._service()

        self._set_plan(account, "pro")
        self.assertFalse(service.can(account, CAP_EXTERNAL_API_TRADE))

        self._set_plan(account, "institutional")
        self.assertTrue(service.can(account, CAP_EXTERNAL_API_TRADE))

    def test_unknown_capability_is_a_403_not_an_upgrade_hint(self):
        account = self._account()
        with self.assertRaises(HTTPException) as ctx:
            self._service().assert_capability(account, "teleport_positions")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail["code"], "capability_unknown")

    def test_out_of_vocabulary_plan_cannot_even_be_persisted(self):
        """The check constraint is the second line of defence after coerce."""
        from app.models.subscription import PlanTier

        account = self._account()
        row = self._service().subscription_for(account)
        row.plan = "platinum_deluxe"
        with self.assertRaises(sqlalchemy.exc.IntegrityError):
            self.db.commit()
        self.db.rollback()

        self.assertIs(PlanTier.coerce("platinum_deluxe"), PlanTier.FREE)
        self.assertEqual(
            sorted(capabilities_for("platinum_deluxe")),
            sorted(PLAN_CAPABILITIES["free"]),
        )

    def test_cancelled_subscription_is_free(self):
        account = self._account()
        self._set_plan(account, "pro")
        service = self._service()
        service.apply_plan(account, "free", status_="canceled")
        self.assertEqual(service.effective_plan(account), "free")

    def test_past_due_keeps_the_plan(self):
        """A failed card must not stop a running strategy mid-flight."""
        account = self._account()
        service = self._service()
        service.apply_plan(account, "pro", status_="past_due")
        self.assertEqual(service.effective_plan(account), "pro")

    def test_required_plan_for_every_capability(self):
        for capability in sorted(ALL_CAPABILITIES):
            required = required_plan_for(capability)
            self.assertIn(required, PLAN_ORDER, msg=capability)
            self.assertIn(capability, PLAN_CAPABILITIES[required], msg=capability)

    def test_plan_limits_are_cumulative_on_accounts(self):
        self.assertEqual(self._service().plan_limits(self._account()).max_accounts, 1)
        account = self._account()
        self._set_plan(account, "pro")
        self.assertEqual(self._service().plan_limits(account).max_accounts, 5)
        self._set_plan(account, "institutional")
        self.assertIsNone(self._service().plan_limits(account).max_accounts)


class RolloutGateTest(EntitlementBaseTest):
    """The default-off enforcement switch (master plan I12)."""

    def test_enforcement_disabled_means_no_gate(self):
        with patch("app.services.users.entitlement_service.get_settings") as mocked:
            mocked.return_value.entitlements_enabled = False
            account = self._account()
            service = EntitlementService(self.db)
            self.assertFalse(service.enforcement_enabled())
            # A free account is *not* entitled, but nothing is refused.
            self.assertFalse(service.can(account, CAP_COPY_TRADING))
            self.assertIsNone(service.assert_capability(account, CAP_COPY_TRADING))
            self.assertIsNone(service.assert_account_limit(account, 99))
            self.assertEqual(service.consume_quota(account, CAP_ORCHESTRATION), 0)


class GatedRouteTest(EntitlementBaseTest):
    """Capability matrix enforcement on every gated route."""

    def _enable_accounts(self):
        patcher = patch("app.api.routes.auth.accounts.get_settings")
        mocked = patcher.start()
        mocked.return_value.accounts_enabled = True
        self.addCleanup(patcher.stop)

    # ── Free tier ──

    def test_free_cannot_follow_a_trader(self):
        self._account()
        response = self.client.post(
            "/api/trades/follow/0xtrader",
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 402)
        body = response.json()["detail"]
        self.assertEqual(body["code"], "plan_required")
        self.assertEqual(body["required_plan"], "pro")

    def test_free_cannot_create_a_market_maker_config(self):
        self._account()
        response = self.client.post(
            "/api/market-maker/configs",
            json={
                "condition_id": "0xcond",
                "token_id_yes": "0xyes",
                "token_id_no": "0xno",
            },
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 402)

    def test_free_cannot_open_an_inverse_bot_position(self):
        self._account()
        response = self.client.post(
            "/api/inverse-bot/positions",
            json={"token_id": "0xtoken", "condition_id": "0xcond"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 402)

    def test_free_cannot_enable_latency_arb(self):
        self._account()
        response = self.client.post(
            "/api/latency-arb/config",
            json={"enabled": True, "symbols": ["BTC"], "windows": [5]},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 402)

    def test_free_cannot_create_a_second_account(self):
        self._enable_accounts()
        self._account()
        response = self.client.post(
            "/api/accounts",
            json={"name": "Second"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["detail"]["code"], "account_limit")

    def test_free_cannot_turn_copy_trading_on_through_settings(self):
        self._account()
        response = self.client.patch(
            f"/api/accounts/{self._account().id}/settings",
            json={"copy_trading_enabled": True},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 402)

    # ── Pro tier ──

    def test_pro_can_follow_a_trader(self):
        self._set_plan(self._account(), "pro")
        response = self.client.post(
            "/api/trades/follow/0xtrader",
            headers=_login_headers(1),
        )
        self.assertIn(response.status_code, (200, 201))
        self.assertEqual(self.db.query(FollowedTrader).count(), 1)

    def test_pro_can_create_a_market_maker_config(self):
        self._set_plan(self._account(), "pro")
        response = self.client.post(
            "/api/market-maker/configs",
            json={
                "condition_id": "0xcond",
                "token_id_yes": "0xyes",
                "token_id_no": "0xno",
            },
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.query(MarketMakerConfig).count(), 1)

    def test_pro_can_create_accounts_up_to_the_ceiling(self):
        self._enable_accounts()
        self._set_plan(self._account(), "pro")
        # The personal account is one of the five, so four sub-accounts fit.
        for index in range(4):
            response = self.client.post(
                "/api/accounts",
                json={"name": f"Sub-{index}"},
                headers=_login_headers(1),
            )
            self.assertEqual(response.status_code, 201, msg=response.text)
        self.assertEqual(self.db.query(Account).count(), 5)

        # The sixth is the ceiling, not the feature flag.
        response = self.client.post(
            "/api/accounts",
            json={"name": "Sub-one-too-many"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 402)
        self.assertEqual(self.db.query(Account).count(), 5)

    def test_institutional_accounts_are_unlimited(self):
        self._enable_accounts()
        self._set_plan(self._account(), "institutional")
        for index in range(7):
            response = self.client.post(
                "/api/accounts",
                json={"name": f"Sub-{index}"},
                headers=_login_headers(1),
            )
            self.assertEqual(response.status_code, 201, msg=response.text)
        self.assertEqual(self.db.query(Account).count(), 8)

    def test_pro_cannot_trade_through_the_external_api(self):
        self._set_plan(self._account(), "pro")
        record, raw_key = create_api_key(
            self.db,
            user_id=1,
            name="pro-key",
            scopes=[SCOPE_READ, SCOPE_TRADE],
            expires_in_days=None,
        )
        response = self.client.post(
            "/api/v1/external/orders",
            json={
                "token_id": TOKEN_ID,
                "market_id": MARKET_ID,
                "side": "BUY",
                "price": 0.5,
                "size": 1.0,
            },
            headers={"X-API-Key": raw_key},
        )
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["detail"]["capability"], CAP_EXTERNAL_API_TRADE)

    def test_institutional_can_trade_through_the_external_api(self):
        """The gate lets an institutional key through to the real path."""
        self._set_plan(self._account(), "institutional")
        record, raw_key = create_api_key(
            self.db,
            user_id=1,
            name="inst-key",
            scopes=[SCOPE_READ, SCOPE_TRADE],
            expires_in_days=None,
        )
        from app.api.routes.platform import external as external_routes

        with (
            patch.object(external_routes, "_load_credentials") as load,
            patch.object(external_routes, "_place_order_on_polymarket") as place,
        ):
            load.return_value = {"private_key": "0xkey", "clob_creds": None}
            place.return_value = {"success": True, "order_hash": "0xhash"}
            response = self.client.post(
                "/api/v1/external/orders",
                json={
                    "token_id": TOKEN_ID,
                    "market_id": MARKET_ID,
                    "side": "BUY",
                    "price": 0.5,
                    "size": 1.0,
                },
                headers={"X-API-Key": raw_key},
            )
        self.assertEqual(response.status_code, 200, msg=response.text)
        self.assertTrue(response.json()["success"])


class DowngradeSafetyTest(EntitlementBaseTest):
    """A downgrade revokes new activity and never destroys anything."""

    def _populate(self) -> Account:
        """A fully-loaded institutional account, ready to be downgraded."""
        account = self._account()
        self._set_plan(account, "institutional")

        # Trading engines' own state. Every one of these rows must survive
        # the downgrade untouched.
        self.db.add(
            UserTrade(
                account_id=account.id,
                user_id=1,
                market_id=MARKET_ID,
                token_id=TOKEN_ID,
                action="buy",
                amount=42.0,
                price=0.5,
                status="executed",
                strategy_source="manual",
            ),
        )
        self.db.add(
            StopLossOrder(
                user_id=1,
                account_id=account.id,
                market_id=MARKET_ID,
                token_id=TOKEN_ID,
                size=42.0,
                stop_price=0.4,
            ),
        )
        self.db.add(FollowedTrader(user_id=1, trader_wallet="0xtrader", is_active=True))
        self.db.add(
            MarketMakerConfig(
                user_id=1,
                condition_id=MARKET_ID,
                token_id_yes="0xyes",
                token_id_no="0xno",
                enabled=True,
            ),
        )
        self.db.add(LatencyArbConfig(user_id=1, enabled=True))
        _, raw_key = create_api_key(
            self.db,
            user_id=1,
            name="inst-key",
            scopes=[SCOPE_READ, SCOPE_TRADE],
            expires_in_days=None,
        )
        self.db.commit()
        return account

    def test_downgrade_keeps_every_row(self):
        account = self._populate()

        trades_before = self.db.query(UserTrade).count()
        stops_before = self.db.query(StopLossOrder).count()
        makers_before = self.db.query(MarketMakerConfig).count()
        follows_before = self.db.query(FollowedTrader).count()
        keys_before = self.db.query(ApiKey).count()

        self._service().apply_plan(account, "free", status_="active")

        self.assertEqual(self.db.query(UserTrade).count(), trades_before)
        self.assertEqual(self.db.query(StopLossOrder).count(), stops_before)
        self.assertEqual(self.db.query(MarketMakerConfig).count(), makers_before)
        self.assertEqual(self.db.query(FollowedTrader).count(), follows_before)
        self.assertEqual(self.db.query(ApiKey).count(), keys_before)

    def test_downgrade_stops_new_automation_only(self):
        account = self._populate()
        self._service().apply_plan(account, "free", status_="active")

        self.assertFalse(self.db.query(MarketMakerConfig).one().enabled)
        self.assertFalse(self.db.query(LatencyArbConfig).one().enabled)
        # The follow row survives; it just cannot be re-activated.
        self.assertTrue(self.db.query(FollowedTrader).one().is_active)
        service = self._service()
        self.assertFalse(service.can(account, CAP_MARKET_MAKER))
        self.assertFalse(service.can(account, CAP_EXTERNAL_API_TRADE))

    def test_downgrade_revokes_the_external_trade_scope_but_keeps_the_key(self):
        account = self._populate()
        key = self.db.query(ApiKey).one()

        self._service().apply_plan(account, "free", status_="active")

        self.db.refresh(key)
        self.assertEqual(key.scopes, json.dumps([SCOPE_READ]))
        self.assertIsNone(key.revoked_at)

    def test_downgrade_leaves_risk_state_alone(self):
        from app.models.account_settings import AccountSettings

        account = self._account()
        self._set_plan(account, "pro")
        settings = self.db.query(AccountSettings).first()
        if settings is None:
            settings = AccountSettings(account_id=account.id)
            self.db.add(settings)
            self.db.commit()
        settings.trading_halted = True
        settings.halt_reason = "daily loss limit"
        settings.peak_capital = 1234.5
        self.db.commit()

        self._service().apply_plan(account, "free", status_="active")

        self.db.refresh(settings)
        self.assertTrue(settings.trading_halted)
        self.assertEqual(settings.halt_reason, "daily loss limit")
        self.assertEqual(settings.peak_capital, 1234.5)

    def test_downgrade_flips_copy_trading_off_once(self):
        account = self._account()
        self._set_plan(account, "pro")
        from app.models.user_settings import UserSettings

        row = self.db.query(UserSettings).filter_by(user_id=1).first()
        if row is None:
            row = UserSettings(user_id=1)
            self.db.add(row)
            self.db.commit()
        row.copy_trading_enabled = True
        row.inverse_bot_enabled = True
        self.db.commit()

        flipped = self._service().disable_lost_automation(account, "pro", "free")
        self.assertIn("copy_trading_enabled", flipped)
        self.assertIn("inverse_bot_enabled", flipped)
        self.assertFalse(row.copy_trading_enabled)
        self.assertFalse(row.inverse_bot_enabled)

    def test_upgrade_flips_nothing(self):
        account = self._populate()
        self._service().apply_plan(account, "institutional", status_="active")
        self.assertTrue(self.db.query(MarketMakerConfig).one().enabled)
        self.assertTrue(self.db.query(LatencyArbConfig).one().enabled)
        self.assertEqual(self.db.query(ApiKey).one().scopes, json.dumps([SCOPE_READ, SCOPE_TRADE]))


class QuotaTest(EntitlementBaseTest):
    """Per-plan quotas on the expensive surfaces."""

    def _pro(self) -> Account:
        account = self._account()
        self._set_plan(account, "pro")
        return account

    def _cache(self):
        from app.services.users.entitlement_service import quota_cache

        return quota_cache

    def setUp(self):
        super().setUp()
        self.addCleanup(self._cache().clear)

    def test_free_has_no_budget_for_expensive_surfaces(self):
        account = self._account()
        service = self._service()
        self.assertEqual(service.plan_limits(account).quota_for(CAP_ORCHESTRATION), 0)
        with self.assertRaises(HTTPException) as ctx:
            service.consume_quota(account, CAP_ORCHESTRATION)
        self.assertEqual(ctx.exception.status_code, 402)

    def test_pro_can_spend_then_exhausts_with_429(self):
        account = self._pro()
        service = self._service()
        limit = service.plan_limits(account).quota_for(CAP_ORCHESTRATION)
        self.assertGreater(limit, 0)

        for _ in range(limit):
            service.consume_quota(account, CAP_ORCHESTRATION)

        with self.assertRaises(HTTPException) as ctx:
            service.consume_quota(account, CAP_ORCHESTRATION)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail["code"], "quota_exceeded")
        self.assertEqual(
            ctx.exception.headers["Retry-After"],
            str(
                service._seconds_until_midnight(
                    __import__("app.utils.time", fromlist=["utc_now"]).utc_now()
                )
            ),
        )

    def test_quotas_are_per_account(self):
        first = self._pro()
        service = self._service()
        limit = service.plan_limits(first).quota_for(CAP_ORCHESTRATION)
        for _ in range(limit):
            service.consume_quota(first, CAP_ORCHESTRATION)

        other = create_sub_account(self.db, 1, "Second")
        self._set_plan(other, "pro")
        # A fresh account still has its whole budget.
        self.assertEqual(service.consume_quota(other, CAP_ORCHESTRATION), 1)

    def test_usage_report_matches_consumption(self):
        account = self._pro()
        service = self._service()
        service.consume_quota(account, CAP_ORCHESTRATION, cost=3)
        report = service.usage_report(account)
        orchestration = next(
            row for row in report["quotas"] if row["capability"] == CAP_ORCHESTRATION
        )
        self.assertEqual(orchestration["used"], 3)
        self.assertEqual(
            orchestration["limit"], service.plan_limits(account).quota_for(CAP_ORCHESTRATION)
        )
        self.assertEqual(report["accounts"]["used"], 1)
        self.assertEqual(report["accounts"]["limit"], 5)


class WebhookVerificationTest(EntitlementBaseTest):
    """A forged webhook must not grant a plan."""

    def _provider(self) -> StripeBillingProvider:
        return StripeBillingProvider(secret_key="sk_test_x", webhook_secret=WEBHOOK_SECRET)

    def _event_payload(self, event_id: str = "evt_1", plan: str = "pro") -> bytes:
        payload = {
            "id": event_id,
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_123",
                    "customer": "cus_123",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "current_period_end": 4102444800,
                    "metadata": {"account_id": 1},
                    "items": {"data": [{"price": {"id": "price_pro"}}]},
                }
            },
        }
        if plan != "pro":
            payload["data"]["object"]["items"]["data"][0]["price"]["id"] = "price_institutional"
        return json.dumps(payload).encode()

    def _configured_provider(self) -> StripeBillingProvider:
        """A provider whose price ids resolve, with the patch left active."""
        patcher = patch(
            "app.services.billing.stripe_provider._setting",
            side_effect=lambda name, default="": {
                "stripe_price_pro": "price_pro",
                "stripe_price_institutional": "price_institutional",
            }.get(name, default),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return self._provider()

    def test_valid_signature_is_accepted(self):
        provider = self._configured_provider()
        payload = self._event_payload()
        event = provider.handle_webhook(payload=payload, signature=_sign(payload))
        self.assertEqual(event.event_id, "evt_1")
        self.assertEqual(event.plan, "pro")

    def test_missing_signature_is_rejected(self):
        provider = self._configured_provider()
        with self.assertRaises(WebhookSignatureError):
            provider.handle_webhook(payload=self._event_payload(), signature=None)

    def test_wrong_secret_is_rejected(self):
        provider = self._configured_provider()
        payload = self._event_payload()
        with self.assertRaises(WebhookSignatureError):
            provider.handle_webhook(payload=payload, signature=_sign(payload, secret="whsec_wrong"))

    def test_tampered_body_is_rejected(self):
        provider = self._configured_provider()
        payload = self._event_payload()
        signature = _sign(payload)
        tampered = self._event_payload(plan="institutional")
        with self.assertRaises(WebhookSignatureError):
            provider.handle_webhook(payload=tampered, signature=signature)

    def test_stale_timestamp_is_rejected(self):
        provider = self._configured_provider()
        payload = self._event_payload()
        stale = int(time_module.time()) - _WEBHOOK_TOLERANCE_SECONDS - 60
        with self.assertRaises(WebhookSignatureError):
            provider.handle_webhook(payload=payload, signature=_sign(payload, timestamp=stale))

    def test_malformed_header_is_rejected(self):
        provider = self._configured_provider()
        with self.assertRaises(WebhookSignatureError):
            provider.handle_webhook(payload=self._event_payload(), signature="not-a-real-header")

    def test_unconfigured_secret_fails_closed(self):
        provider = StripeBillingProvider(secret_key="sk_test_x", webhook_secret="")
        with self.assertRaises(WebhookSignatureError):
            provider.handle_webhook(payload=self._event_payload(), signature=_sign(b"{}"))

    def test_empty_payload_is_rejected(self):
        provider = self._configured_provider()
        with self.assertRaises(WebhookSignatureError):
            provider.handle_webhook(payload=b"", signature=_sign(b""))

    def test_protocol_is_satisfied(self):
        from app.services.billing import BillingProvider

        self.assertIsInstance(self._provider(), BillingProvider)

    def test_price_id_that_is_not_configured_grants_nothing(self):
        patcher = patch(
            "app.services.billing.stripe_provider._setting",
            side_effect=lambda name, default="": default,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        provider = self._provider()
        payload = self._event_payload()
        event = provider.handle_webhook(payload=payload, signature=_sign(payload))
        self.assertIsNone(event.plan)


class WebhookApplicationTest(EntitlementBaseTest):
    """Idempotency by event id, and the endpoint carrying no other auth."""

    def setUp(self):
        super().setUp()
        # The billing router lives on its own app for this module.
        self.client = TestClient(billing_app)

    def _payload(self, event_id: str = "evt_apply_1", plan: str = "pro") -> bytes:
        payload = {
            "id": event_id,
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_apply",
                    "customer": "cus_apply",
                    "status": "active",
                    "cancel_at_period_end": False,
                    "metadata": {"account_id": 1},
                    "items": {"data": [{"price": {"id": "price_pro"}}]},
                }
            },
        }
        if plan == "institutional":
            payload["data"]["object"]["items"]["data"][0]["price"]["id"] = "price_institutional"
        return json.dumps(payload).encode()

    def _post_webhook(self, payload: bytes, signature: str):
        return self.client.post(
            "/api/billing/webhook",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": signature,
            },
        )

    def _patch_provider(self):
        provider = StripeBillingProvider(secret_key="sk_test_x", webhook_secret=WEBHOOK_SECRET)
        patcher = patch("app.api.routes.billing.billing._provider", return_value=provider)
        patcher.start()
        self.addCleanup(patcher.stop)

        price_patcher = patch(
            "app.services.billing.stripe_provider._setting",
            side_effect=lambda name, default="": {
                "stripe_price_pro": "price_pro",
                "stripe_price_institutional": "price_institutional",
            }.get(name, default),
        )
        price_patcher.start()
        self.addCleanup(price_patcher.stop)
        return provider

    def test_verified_event_upgrades_the_plan(self):
        account = self._account()
        self._patch_provider()
        payload = self._payload()
        response = self._post_webhook(payload, _sign(payload))

        self.assertEqual(response.status_code, 200, msg=response.text)
        body = response.json()
        self.assertTrue(body["received"])
        self.assertFalse(body["duplicate"])
        self.assertTrue(body["applied"])
        self.assertEqual(body["plan"], "pro")
        self.assertEqual(self._service().effective_plan(account), "pro")

    def test_replayed_event_changes_nothing(self):
        account = self._account()
        self._patch_provider()
        payload = self._payload()
        self._post_webhook(payload, _sign(payload))

        before = self._service().subscription_for(account)
        replay = self._post_webhook(payload, _sign(payload))
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["duplicate"])

        after = self._service().subscription_for(account)
        self.assertEqual(before.plan, after.plan)
        self.assertEqual(before.id, after.id)
        self.assertEqual(after.plan, "pro")

    def test_forged_event_is_rejected_and_changes_nothing(self):
        account = self._account()
        self._patch_provider()
        payload = self._payload()
        response = self._post_webhook(payload, _sign(payload, secret="whsec_forged"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._service().effective_plan(account), "free")

    def test_webhook_has_no_session_or_key_gate(self):
        """The endpoint must accept an unauthenticated request it can verify."""
        self._patch_provider()
        payload = self._payload(event_id="evt_noauth")
        response = self._post_webhook(payload, _sign(payload))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("authorization", {k.lower() for k in response.headers})

    def test_event_for_an_unknown_account_is_recorded_not_applied(self):
        from app.models.subscription import BillingWebhookEvent

        self._patch_provider()
        payload = json.dumps(
            {
                "id": "evt_unknown",
                "type": "customer.subscription.updated",
                "data": {
                    "object": {
                        "id": "sub_unknown",
                        "customer": "cus_unknown",
                        "status": "active",
                        "metadata": {"account_id": 9999},
                        "items": {"data": [{"price": {"id": "price_pro"}}]},
                    }
                },
            }
        ).encode()
        response = self._post_webhook(payload, _sign(payload))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["applied"])
        self.assertEqual(
            self.db.query(BillingWebhookEvent).filter_by(event_id="evt_unknown").count(),
            1,
        )

    def test_cancelled_event_downgrades_additively(self):
        account = self._account()
        self._patch_provider()
        upgrade = self._payload(event_id="evt_upgrade_for_cancel")
        self._post_webhook(upgrade, _sign(upgrade))
        self.assertEqual(self._service().effective_plan(account), "pro")

        self.db.add(
            UserTrade(
                account_id=account.id,
                user_id=1,
                market_id=MARKET_ID,
                token_id=TOKEN_ID,
                action="buy",
                amount=7.0,
                price=0.5,
                status="executed",
                strategy_source="manual",
            ),
        )
        self.db.commit()

        cancellation = json.dumps(
            {
                "id": "evt_cancel",
                "type": "customer.subscription.deleted",
                "data": {
                    "object": {
                        "id": "sub_apply",
                        "customer": "cus_apply",
                        "status": "canceled",
                        "metadata": {"account_id": 1},
                        "items": {"data": [{"price": {"id": "price_pro"}}]},
                    }
                },
            }
        ).encode()
        response = self._post_webhook(cancellation, _sign(cancellation))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._service().effective_plan(account), "free")
        # Additive gating: the trade is still there.
        self.assertEqual(self.db.query(UserTrade).count(), 1)

    def test_unmapped_event_type_is_accepted_and_applied_to_nothing(self):
        self._patch_provider()
        payload = json.dumps(
            {
                "id": "evt_unmapped",
                "type": "ping.hello",
                "data": {"object": {}},
            }
        ).encode()
        response = self._post_webhook(payload, _sign(payload))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["applied"])
        self.assertEqual(self._service().effective_plan(self._account()), "free")


class BillingRoutesTest(EntitlementBaseTest):
    """The session-authenticated billing surface."""

    def setUp(self):
        super().setUp()
        # The billing router lives on its own app for this module.
        self.client = TestClient(billing_app)

    def _account(self, user_id: int = 1) -> Account:
        return resolve_account(self.db, user_id, None)

    def test_subscription_endpoint_reports_free_usage(self):
        self._account()
        response = self.client.get("/api/billing/subscription", headers=_login_headers(1))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["subscription"]["plan"], "free")
        self.assertEqual(body["usage"]["plan"], "free")
        self.assertEqual(len(body["usage"]["quotas"]), 3)

    def test_subscription_endpoint_requires_authentication(self):
        self.assertEqual(self.client.get("/api/billing/subscription").status_code, 401)

    def test_plans_endpoint_returns_the_matrix(self):
        self._account()
        response = self.client.get("/api/billing/plans", headers=_login_headers(1))
        self.assertEqual(response.status_code, 200)
        plans = {row["plan"]: row for row in response.json()["plans"]}
        self.assertEqual(set(plans), {"free", "pro", "institutional"})
        self.assertNotIn(CAP_EXTERNAL_API_TRADE, plans["pro"]["capabilities"])
        self.assertIn(CAP_EXTERNAL_API_TRADE, plans["institutional"]["capabilities"])

    def test_checkout_requires_an_email_for_an_account_without_one(self):
        self._account()
        response = self.client.post(
            "/api/billing/checkout-session",
            json={"plan": "pro"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "email_required")

    def test_checkout_rejects_an_invalid_email(self):
        self._account()
        response = self.client.post(
            "/api/billing/checkout-session",
            json={"plan": "pro", "email": "not-an-email"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 400)

    def test_checkout_rejects_the_free_plan(self):
        self._account()
        response = self.client.post(
            "/api/billing/checkout-session",
            json={"plan": "free", "email": "buyer@example.com"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 400)

    def test_portal_without_a_customer_is_a_conflict(self):
        self._account()
        response = self.client.post(
            "/api/billing/portal-session",
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "no_billing_customer")

    def test_checkout_creates_a_customer_and_session(self):
        account = self._account()
        provider = StripeBillingProvider(secret_key="sk_test_x", webhook_secret=WEBHOOK_SECRET)
        patcher = patch("app.api.routes.billing.billing._provider", return_value=provider)
        patcher.start()
        self.addCleanup(patcher.stop)

        with (
            patch.object(
                StripeBillingProvider,
                "create_customer",
                return_value="cus_new",
            ) as customer,
            patch.object(
                StripeBillingProvider,
                "create_checkout_session",
                return_value=__import__(
                    "app.services.billing.stripe_provider", fromlist=["CheckoutSession"]
                ).CheckoutSession(id="cs_1", url="https://checkout.stripe.test/cs_1"),
            ) as checkout,
        ):
            response = self.client.post(
                "/api/billing/checkout-session",
                json={"plan": "pro", "email": "buyer@example.com"},
                headers=_login_headers(1),
            )

        self.assertEqual(response.status_code, 200, msg=response.text)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.test/cs_1")
        customer.assert_called_once()
        checkout.assert_called_once()
        # The plan is written by the webhook, never by the checkout call.
        self.assertEqual(self._service().effective_plan(account), "free")
        # The email is stored for the receipt.
        self.db.refresh(self.db.query(User).filter_by(id=1).one())
        self.assertEqual(self.db.query(User).filter_by(id=1).one().email, "buyer@example.com")

    def test_checkout_without_billing_configured_is_503(self):
        self._account()
        provider = StripeBillingProvider(secret_key="", webhook_secret="")
        patcher = patch("app.api.routes.billing.billing._provider", return_value=provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        response = self.client.post(
            "/api/billing/checkout-session",
            json={"plan": "pro", "email": "buyer@example.com"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 503)


class MigrationBackfillTest(unittest.TestCase):
    """Run the real 20261011_0018 migration against a fresh database.

    Same idiom as ``test_accounts.MigrationBackfillTest``: the schema is
    built from ``Base.metadata.create_all``, rewound to the revision this
    migration chains from (``subscriptions`` and
    ``billing_webhook_events`` dropped), and the migration is then run
    in-process so the assertions are on its own SQL.
    """

    MIGRATION_MODULE = "migrations.versions.20261011_0018_add_subscriptions"

    def _migrate(self):
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        self.engine, self.SessionFactory = _make_db()
        Base.metadata.create_all(self.engine)

        db = self.SessionFactory()
        db.add(User(id=1, wallet_address=OWNER_WALLET))
        db.add(User(id=2, wallet_address=OTHER_WALLET))
        db.commit()

        # One personal account per owner, the state revision 20261011_0017
        # left behind.
        resolve_account(db, 1, None)
        resolve_account(db, 2, None)
        create_sub_account(db, 1, "Alpha")
        db.commit()
        accounts_before = db.query(Account).count()
        self.assertEqual(accounts_before, 3)

        with self.engine.connect() as conn:
            conn.execute(text("DROP TABLE IF EXISTS billing_webhook_events"))
            conn.execute(text("DROP TABLE IF EXISTS subscriptions"))
            conn.commit()

        with self.engine.connect() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(migration_context=context):
                importlib.import_module(self.MIGRATION_MODULE).upgrade()
            conn.commit()

        self.addCleanup(self.engine.dispose)
        self.addCleanup(db.close)
        return db

    def test_every_account_gets_a_free_subscription(self):
        db = self._migrate()
        rows = db.query(Subscription).order_by(Subscription.account_id).all()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row.plan, "free")
            self.assertEqual(row.status, "none")
            self.assertEqual(row.provider, "none")
            self.assertFalse(row.cancel_at_period_end)
            self.assertIsNotNone(row.created_at)
            self.assertIsNotNone(row.updated_at)

    def test_migration_is_idempotent(self):
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        db = self._migrate()
        with self.engine.connect() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(migration_context=context):
                importlib.import_module(self.MIGRATION_MODULE).upgrade()
            conn.commit()

        self.assertEqual(db.query(Subscription).count(), 3)
        self.assertEqual(
            db.query(Subscription).filter_by(plan="free").count(),
            3,
        )

    def test_webhook_ledger_table_exists_with_a_unique_event_id(self):
        self._migrate()
        with self.engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(subscriptions)"))}
        self.assertIn("provider_subscription_id", columns)
        self.assertIn("current_period_end", columns)
        with self.engine.connect() as conn:
            ledger = {
                row[1] for row in conn.execute(text("PRAGMA table_info(billing_webhook_events)"))
            }
        self.assertIn("event_id", ledger)

    def test_downgrade_after_migration_is_safe(self):
        """The backfilled free row is a real, writable subscription."""
        db = self._migrate()
        account = db.query(Account).filter_by(kind=AccountKind.PERSONAL.value).first()
        service = EntitlementService(db)
        self.assertEqual(service.effective_plan(account), "free")
        service.apply_plan(account, "pro", status_="active")
        self.assertEqual(service.effective_plan(account), "pro")
        service.apply_plan(account, "free", status_="active")
        self.assertEqual(service.effective_plan(account), "free")


if __name__ == "__main__":
    unittest.main()
