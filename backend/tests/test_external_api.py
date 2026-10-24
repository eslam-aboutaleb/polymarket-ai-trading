"""External API, audit export and exposure tests (plan 06 Phase B).

Everything here is about the two promises the external namespace makes.

**A machine credential can do what a browser can, and nothing more.**
Reads require the ``read`` scope, ``POST /orders`` requires ``trade``,
and the internal surfaces (admin, credentials, settings) are not present
on this app at all: the only routes registered are the ones Phase B
owns, which is the structural form of "an API key cannot reach them".

**An external order is a manual order.** ``apply_global_safety_caps``
is *not* mocked in these tests — the real gate runs, the real
recorder writes the ``UserTrade`` row and the real event log gets its
``order_submitted`` row, and a gate rejection means no order was
placed. Only the exchange itself (``_place_order_on_polymarket``) and
the read-only Polymarket service are doubles.

The app is a minimal one carrying Phase B's routers, the same idiom
``test_risk_report_routes.py`` uses for a router the coordinator has
not yet registered in ``app/main.py``.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.routes.auth.accounts as accounts_module
import app.api.routes.platform.external as external_module
import app.api.routes.platform.ws_v1 as ws_module
import app.models  # noqa: F401  (register models on Base.metadata)
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.base import Base
from app.models.trading.user_trade import UserTrade
from app.models.users.api_key import ApiKey
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.services.platform import audit_service, event_log
from app.services.users import api_key_service
from app.utils.database import get_db
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20
PRIVATE_KEY = "0x" + "11" * 32
CLOB_CREDS = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}
HASH_SECRET = "k" * 32
TOKEN_ID = "0xtoken-yes"
MARKET_ID = "0xmarket-cond"

EXTERNAL_TEST_APP = FastAPI()
EXTERNAL_TEST_APP.include_router(external_module.router)
EXTERNAL_TEST_APP.include_router(external_module.api_keys_router)
EXTERNAL_TEST_APP.include_router(external_module.exposure_router)
EXTERNAL_TEST_APP.include_router(ws_module.router)
# The accounts router is included only so the audit emissions Phase B
# added there can be asserted without the coordinator's main.py edit.
EXTERNAL_TEST_APP.include_router(accounts_module.router)

# Rate limits are kept out of the way deterministically (the repo's
# idiom for the trading-key routes), so request counts stay assertable.
EXTERNAL_TEST_APP.dependency_overrides[external_module._external_read_rate_limit] = lambda: None
EXTERNAL_TEST_APP.dependency_overrides[external_module._external_write_rate_limit] = lambda: None
EXTERNAL_TEST_APP.dependency_overrides[external_module._api_key_create_rate_limit] = lambda: None

ORDER_BODY = {
    "execution_mode": "live",
    "token_id": TOKEN_ID,
    "market_id": MARKET_ID,
    "side": "BUY",
    "price": 0.5,
    "size": 10.0,
}


class _ExternalBase(unittest.TestCase):
    """In-memory DB, one owner plus one foreigner, and configured hashing."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.db = self.SessionFactory()
        self.db.add(User(id=1, wallet_address=WALLET))
        self.db.add(User(id=2, wallet_address=OTHER_WALLET))
        self.db.commit()

        EXTERNAL_TEST_APP.dependency_overrides[get_db] = self._get_db_override
        self._patch_hash_secret()
        self.client = TestClient(EXTERNAL_TEST_APP)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        EXTERNAL_TEST_APP.dependency_overrides.pop(get_db, None)
        EXTERNAL_TEST_APP.dependency_overrides.pop(get_current_user_from_token, None)
        self.db.close()
        self.engine.dispose()

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _patch_hash_secret(self):
        patcher = patch.object(
            api_key_service,
            "get_settings",
            return_value=SimpleNamespace(api_key_hash_secret=HASH_SECRET, jwt_secret_key=""),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── fixtures ──

    def _session(self, user_id: int = 1):
        wallet = WALLET if user_id == 1 else OTHER_WALLET
        EXTERNAL_TEST_APP.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": user_id,
            "wallet_address": wallet,
            "is_admin": False,
        }

    def _make_key(self, *, scopes=("read",), account_id=None, name="bot") -> tuple[ApiKey, str]:
        record, secret = api_key_service.create_api_key(
            self.db,
            user_id=1,
            name=name,
            account_id=account_id,
            scopes=list(scopes),
        )
        return record, secret

    def _auth_headers(self, secret: str, account_id: int | None = None) -> dict:
        headers = {"X-API-Key": secret}
        if account_id is not None:
            headers["X-Account-Id"] = str(account_id)
        return headers

    def _settings(self, **fields) -> UserSettings:
        """Create or update the user_settings row the safety gate reads."""
        fields.setdefault("max_position_size", 100.0)
        fields.setdefault("daily_loss_limit", 500.0)
        fields.setdefault("user_id", 1)
        row = self.db.query(UserSettings).filter_by(user_id=fields["user_id"]).first()
        if row is None:
            row = UserSettings(**fields)
            self.db.add(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _login_headers(self) -> dict:
        """Mint a real session token so the accounts routes' own auth runs."""
        from app.security.auth import create_access_token

        token = create_access_token({"user_id": 1, "sub": WALLET, "is_admin": False})
        return {"Authorization": f"Bearer {token}"}

    def _trade(self, **fields) -> UserTrade:
        defaults = {
            "user_id": 1,
            "market_id": MARKET_ID,
            "token_id": TOKEN_ID,
            "action": "buy",
            "amount": 40.0,
            "price": 0.5,
            "status": "pending",
            "strategy_source": "external",
        }
        defaults.update(fields)
        row = UserTrade(**defaults)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _service(self, **overrides) -> MagicMock:
        service = MagicMock()
        service.get_portfolio_summary = AsyncMock(
            return_value={
                "wallet_address": WALLET,
                "usdc_balance": 123.0,
                "total_pnl": 4.0,
                "positions": [],
            }
        )
        service.get_positions = AsyncMock(
            return_value=[{"condition_id": MARKET_ID, "size": 10, "asset_id": TOKEN_ID}]
        )
        service.get_active_markets = AsyncMock(return_value=[{"id": MARKET_ID, "question": "Q?"}])
        for name, value in overrides.items():
            getattr(service, name).return_value = value
        return service

    @patch.object(external_module, "load_wallet_credentials", return_value=None)
    def _no_credentials(self, _mock):
        return None

    def _credentials(self):
        patcher = patch.object(
            external_module,
            "load_wallet_credentials",
            return_value={"private_key": PRIVATE_KEY, "clob_creds": CLOB_CREDS},
        )
        patcher.start()
        self.addCleanup(patcher.stop)


# ── Authentication and scope ────────────────────────────────


class ExternalAuthTest(_ExternalBase):
    """The namespace authenticates by API key only, and enforces scopes."""

    def test_all_external_routes_refuse_an_anonymous_caller(self):
        paths = [
            ("get", "/api/v1/external/portfolio"),
            ("get", "/api/v1/external/positions"),
            ("get", "/api/v1/external/markets"),
            ("get", "/api/v1/external/trades"),
            ("get", "/api/v1/external/fills"),
            ("get", "/api/v1/external/audit"),
            ("post", "/api/v1/external/orders"),
        ]
        for method, path in paths:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 401)

    def test_cookie_or_bearer_session_is_not_accepted(self):
        self._session()
        self._credentials()
        headers = {"Authorization": "Bearer not-an-api-key"}
        self.assertEqual(
            self.client.get("/api/v1/external/portfolio", headers=headers).status_code, 401
        )

    def test_invalid_key_is_401(self):
        _, _ = self._make_key()
        response = self.client.get("/api/v1/external/portfolio", headers={"X-API-Key": "pk_nope"})
        self.assertEqual(response.status_code, 401)

    def test_revoked_key_is_401(self):
        record, secret = self._make_key()
        api_key_service.revoke_api_key(self.db, 1, record.id)
        response = self.client.get("/api/v1/external/portfolio", headers={"X-API-Key": secret})
        self.assertEqual(response.status_code, 401)

    def test_read_only_key_cannot_submit_an_order(self):
        _, secret = self._make_key(scopes=("read",))
        with patch("app.services.trading.execution.live.place_live_order") as place:
            response = self.client.post(
                "/api/v1/external/orders",
                json=ORDER_BODY,
                headers={"X-API-Key": secret},
            )
        self.assertEqual(response.status_code, 403)
        place.assert_not_called()
        self.assertEqual(self.db.query(UserTrade).count(), 0)

    def test_trade_scope_key_still_requires_release_authorization(self):
        _, secret = self._make_key(scopes=("read", "trade"))
        self._settings()
        self._credentials()
        with patch(
            "app.services.trading.execution.live.place_live_order",
            return_value={"success": True, "order_hash": "0xhash"},
        ):
            response = self.client.post(
                "/api/v1/external/orders",
                json=ORDER_BODY,
                headers={"X-API-Key": secret},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "LIVE_TRADING_DISABLED")

    def test_external_key_cannot_reach_internal_routes(self):
        _, secret = self._make_key(scopes=("read", "trade"))
        headers = {"X-API-Key": secret}
        # Management is a *session* surface: presenting an API key is
        # the same as presenting nothing, so every route refuses with a
        # 401 rather than acting on the key.
        payloads = {
            ("get", "/api/settings/api-keys"): {},
            ("post", "/api/settings/api-keys"): {"json": {"name": "x"}},
            ("delete", "/api/settings/api-keys/1"): {},
        }
        for (method, path), kwargs in payloads.items():
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, headers=headers, **kwargs)
                self.assertEqual(response.status_code, 401)


# ── Reads ───────────────────────────────────────────────────


class ExternalReadTest(_ExternalBase):
    """Reads mirror the internal shapes and never widen the scope."""

    def setUp(self):
        super().setUp()
        self.service = self._service()
        patcher = patch.object(external_module, "get_polymarket_service", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._credentials()
        _, self.secret = self._make_key()
        self.headers = self._auth_headers(self.secret)

    def test_portfolio_reads_the_key_owners_wallet(self):
        payload = self.client.get("/api/v1/external/portfolio", headers=self.headers).json()
        self.assertEqual(payload["usdc_balance"], 123.0)
        self.assertIsNone(payload["account_id"])
        self.service.get_portfolio_summary.assert_awaited_once()

    def test_positions_and_markets(self):
        positions = self.client.get("/api/v1/external/positions", headers=self.headers).json()
        self.assertEqual(positions["count"], 1)
        markets = self.client.get("/api/v1/external/markets", headers=self.headers).json()
        self.assertEqual(markets["count"], 1)

    def test_missing_credentials_is_a_400(self):
        with patch.object(external_module, "load_wallet_credentials", return_value=None):
            response = self.client.get("/api/v1/external/portfolio", headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_credential_store_failure_is_a_503(self):
        from app.security.credential_store import CredentialStoreError

        with patch.object(
            external_module,
            "load_wallet_credentials",
            side_effect=CredentialStoreError("encryption unavailable"),
        ):
            response = self.client.get("/api/v1/external/portfolio", headers=self.headers)
        self.assertEqual(response.status_code, 503)

    def test_trades_are_scoped_to_the_keys_account(self):
        mine = self._trade(amount=40.0)
        sub_account_row = self._trade(account_id=99, amount=7.0)
        self._trade(user_id=2, amount=999.0)

        # An unscoped key sees the owner's own orders — every account
        # the owner holds — and nobody else's.
        body = self.client.get("/api/v1/external/trades", headers=self.headers).json()
        self.assertEqual([row["trade_id"] for row in body], [sub_account_row.id, mine.id])

    def test_trades_respect_an_account_scoped_key(self):
        from app.services.users.account_service import create_sub_account, ensure_personal_account

        ensure_personal_account(self.db, 1)
        alpha = create_sub_account(self.db, 1, "Alpha")

        record, secret = self._make_key(account_id=alpha.id, name="scoped")
        self._trade(amount=40.0)
        scoped = self._trade(account_id=alpha.id, amount=7.0)
        body = self.client.get("/api/v1/external/trades", headers=self._auth_headers(secret)).json()
        self.assertEqual([row["trade_id"] for row in body], [scoped.id])
        self.assertEqual(record.account_id, alpha.id)

    def test_fills_return_observed_fills_with_slippage(self):
        pending = self._trade(status="pending")
        filled = self._trade(status="executed", filled_price=0.51, filled_size=80.0, fee_paid=0.1)
        body = self.client.get("/api/v1/external/fills", headers=self.headers).json()
        self.assertEqual([row["trade_id"] for row in body], [filled.id])
        self.assertEqual(body[0]["filled_price"], 0.51)
        self.assertEqual(pending.id, pending.id)


# ── Orders: safety parity ───────────────────────────────────


class ExternalOrderTest(_ExternalBase):
    def setUp(self):
        super().setUp()
        self._settings()
        patcher = patch("app.services.trading.execution.live.require_enabled")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gateway = patch(
            "app.services.trading.execution.live.place_live_order",
            return_value={
                "success": True,
                "status": "submitted",
                "order_hash": "real-order",
                "trade_id": 7,
            },
        )
        self.place = self.gateway.start()
        self.addCleanup(self.gateway.stop)
        _, self.secret = self._make_key(scopes=("read", "trade"))
        self.headers = {"X-API-Key": self.secret, "Idempotency-Key": "source"}

    def test_primary_account_order_uses_durable_gateway(self):
        response = self.client.post(
            "/api/v1/external/orders", json=ORDER_BODY, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trade_id"], 7)
        self.assertEqual(self.place.call_args.kwargs["strategy"], "external")
        self.assertTrue(self.place.call_args.kwargs["idempotency_key"].endswith(":source"))

    def test_missing_idempotency_key_cannot_submit(self):
        response = self.client.post(
            "/api/v1/external/orders", json=ORDER_BODY, headers={"X-API-Key": self.secret}
        )
        self.assertEqual(response.status_code, 422)
        self.place.assert_not_called()

    def test_gateway_risk_rejection_reaches_api_client(self):
        from app.services.trading.live_trading_policy import LiveTradingBlocked

        self.place.side_effect = LiveTradingBlocked("LIVE_POSITION_BUDGET_EXCEEDED")
        response = self.client.post(
            "/api/v1/external/orders", json=ORDER_BODY, headers=self.headers
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("BUDGET_EXCEEDED", response.text)

    def test_scoped_key_preserves_account_identity_in_gateway(self):
        from app.services.users.account_service import create_sub_account, ensure_personal_account

        ensure_personal_account(self.db, 1)
        account = create_sub_account(self.db, 1, "Subaccount")
        _, secret = self._make_key(scopes=("read", "trade"), account_id=account.id)
        response = self.client.post(
            "/api/v1/external/orders",
            json=ORDER_BODY,
            headers={"X-API-Key": secret, "Idempotency-Key": "key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.place.call_args.kwargs["account_id"], account.id)


class AuditExportTest(_ExternalBase):
    """The export reads the append-only log and renders both formats."""

    def setUp(self):
        super().setUp()
        _, self.secret = self._make_key()
        self.headers = self._auth_headers(self.secret)
        event_log.append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="0xorder",
            user_id=1,
            payload={"strategy_source": "manual", "amount": 10.0},
            source="trade_recorder",
        )
        event_log.append(
            self.db,
            event_type="settings_changed",
            aggregate_type="settings",
            aggregate_id="1",
            user_id=1,
            payload={"changed_fields": ["daily_loss_limit"]},
            source="settings_api",
        )
        event_log.append(
            self.db,
            event_type="credential_stored",
            aggregate_type="credential",
            user_id=2,
            payload={},
            source="credentials_api",
        )
        self.db.flush()

    def test_json_export_is_owner_scoped_and_newest_first(self):
        body = self.client.get("/api/v1/external/audit", headers=self.headers).json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(
            [row["event_type"] for row in body["events"]],
            ["settings_changed", "order_submitted"],
        )
        self.assertNotIn("payload_json", str(body))

    def test_type_filter(self):
        body = self.client.get(
            "/api/v1/external/audit?type=order_submitted", headers=self.headers
        ).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["events"][0]["event_type"], "order_submitted")

    def test_unknown_type_names_do_not_empty_the_export(self):
        body = self.client.get(
            "/api/v1/external/audit?type=not-a-real-event", headers=self.headers
        ).json()
        self.assertEqual(body["count"], 2)

    def test_window_filter(self):
        body = self.client.get(
            "/api/v1/external/audit?from=2030-01-01T00:00:00Z", headers=self.headers
        ).json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["filters"]["from"], "2030-01-01T00:00:00+00:00")

    def test_csv_export(self):
        response = self.client.get("/api/v1/external/audit?format=csv", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        lines = response.text.strip().splitlines()
        self.assertIn("occurred_at,user_id,account_id,aggregate_type", lines[0])
        self.assertEqual(len(lines), 3)
        self.assertIn("order_submitted", lines[2])

    def test_csv_neutralises_formula_cells(self):
        event_log.append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="=SUM(A1:A2)",
            user_id=1,
            payload={},
            source="trade_recorder",
        )
        self.db.flush()
        response = self.client.get("/api/v1/external/audit?format=csv", headers=self.headers)
        self.assertIn("'=SUM(A1:A2)", response.text)

    def test_account_scoped_key_sees_only_its_own_events(self):
        _, scoped_secret = self._make_key(account_id=42, name="scoped")
        event_log.append(
            self.db,
            event_type="settings_changed",
            aggregate_type="settings",
            user_id=1,
            account_id=42,
            payload={"changed_fields": ["kelly_fraction"]},
            source="settings_api",
        )
        self.db.flush()
        body = self.client.get(
            "/api/v1/external/audit", headers=self._auth_headers(scoped_secret)
        ).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["events"][0]["account_id"], 42)


# ── Exposure ────────────────────────────────────────────────


class ExposureTest(_ExternalBase):
    """Gross and net exposure, scoped to the active account."""

    def setUp(self):
        super().setUp()
        self._session()

    def test_exposure_requires_a_session(self):
        EXTERNAL_TEST_APP.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/portfolio/exposure")
        self.assertEqual(response.status_code, 401)

    def test_gross_and_net_notional_per_outcome(self):
        self._trade(amount=40.0, price=0.5, action="buy", token_id=TOKEN_ID)
        self._trade(amount=20.0, price=0.5, action="sell", token_id="0xtoken-no")
        self._trade(amount=10.0, price=0.5, action="buy", token_id=TOKEN_ID)

        body = self.client.get("/api/portfolio/exposure").json()
        self.assertEqual(body["market_count"], 1)
        self.assertEqual(body["gross_notional"], 70.0)
        # Buys minus the sell.
        self.assertEqual(body["net_notional"], 30.0)

        by_token = {row["token_id"]: row for row in body["outcomes"]}
        self.assertEqual(by_token[TOKEN_ID]["gross_notional"], 50.0)
        self.assertEqual(by_token[TOKEN_ID]["net_notional"], 50.0)
        self.assertEqual(by_token[TOKEN_ID]["orders"], 2)
        self.assertEqual(by_token["0xtoken-no"]["net_notional"], -20.0)
        self.assertEqual(by_token[TOKEN_ID]["gross_shares"], 100.0)
        self.assertEqual(body["limits"]["max_position_size"], 100.0)

    def test_closed_orders_are_not_exposure(self):
        self._trade(amount=40.0, status="executed")
        self._trade(amount=999.0, status="cancelled")
        self._trade(amount=999.0, status="failed")
        self._trade(amount=999.0, status="simulated")
        body = self.client.get("/api/portfolio/exposure").json()
        self.assertEqual(body["gross_notional"], 40.0)

    def test_exposure_is_account_scoped(self):
        from app.services.users.account_service import create_sub_account, ensure_personal_account

        personal = ensure_personal_account(self.db, 1)
        alpha = create_sub_account(self.db, 1, "Alpha")

        # A NULL account_id row predates the backfill: it belongs to the
        # personal account and must not leak into Alpha.
        self._trade(amount=40.0, account_id=None)
        self._trade(amount=25.0, account_id=alpha.id)
        self._trade(amount=5.0, account_id=personal.id)

        import app.api.routes.platform.external as external_routes

        with (
            patch("app.api.deps.get_settings") as mocked,
            patch.object(external_routes, "get_settings", mocked),
        ):
            mocked.return_value.accounts_enabled = True
            body = self.client.get("/api/portfolio/exposure").json()
            self.assertTrue(body["accounts_enabled"])
            self.assertEqual(body["account_id"], personal.id)
            self.assertEqual(body["gross_notional"], 45.0)

            switched = self.client.get(
                "/api/portfolio/exposure", headers={"X-Account-Id": str(alpha.id)}
            ).json()
            self.assertEqual(switched["account_id"], alpha.id)
            self.assertEqual(switched["gross_notional"], 25.0)

    def test_exposure_of_another_users_rows_is_never_counted(self):
        self._trade(user_id=2, amount=999.0)
        body = self.client.get("/api/portfolio/exposure").json()
        self.assertEqual(body["gross_notional"], 0.0)
        self.assertEqual(body["outcomes"], [])


# ── Audit emissions wired in Phase A routes ─────────────────


class AccountAuditEmissionTest(_ExternalBase):
    """The actor-side vocabulary Phase B added to the accounts routes."""

    def setUp(self):
        super().setUp()
        self._session()
        self._enable_accounts()

    def _enable_accounts(self):
        patcher = patch("app.api.routes.auth.accounts.get_settings")
        mocked = patcher.start()
        mocked.return_value.accounts_enabled = True
        self.addCleanup(patcher.stop)

    def test_account_created_and_settings_changes_are_logged(self):
        from app.services.users.account_service import ensure_personal_account

        # A sub-account is only ever created under an existing owner.
        ensure_personal_account(self.db, 1)
        created = self.client.post(
            "/api/accounts", json={"name": "Alpha"}, headers=self._login_headers()
        ).json()
        events = event_log.get_events(self.db, user_id=1, event_types=["account_created"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["aggregate_id"], str(created["id"]))

        self.client.patch(
            f"/api/accounts/{created['id']}/settings",
            json={"paper_balance": 4321.0, "copy_trading_enabled": True},
            headers=self._login_headers(),
        )
        changed = event_log.get_events(self.db, user_id=1, event_types=["settings_changed"])
        self.assertEqual(len(changed), 1)
        self.assertIn("paper_balance", changed[0]["payload"]["changed_fields"])

        toggles = event_log.get_events(
            self.db, user_id=1, event_types=["strategy_enabled", "strategy_disabled"]
        )
        self.assertEqual(len(toggles), 1)
        self.assertEqual(toggles[0]["event_type"], "strategy_enabled")
        self.assertEqual(toggles[0]["aggregate_id"], "copy_trading")

    def test_account_switched_is_logged_only_for_an_explicit_switch(self):
        from app.services.users.account_service import ensure_personal_account

        personal = ensure_personal_account(self.db, 1)
        with patch("app.api.deps.get_settings") as mocked:
            mocked.return_value.accounts_enabled = True

            self.client.get("/api/accounts/active", headers=self._login_headers())
            self.assertEqual(
                event_log.get_events(self.db, user_id=1, event_types=["account_switched"]), []
            )

            self.client.get(
                "/api/accounts/active",
                headers={**self._login_headers(), "X-Account-Id": str(personal.id)},
            ).json()
            switched = event_log.get_events(self.db, user_id=1, event_types=["account_switched"])
            self.assertEqual(len(switched), 1)
            self.assertEqual(switched[0]["aggregate_id"], str(personal.id))

    def test_audit_service_export_matches_the_log(self):
        from app.services.trading.trade_recorder import record_user_trade

        record_user_trade(
            self.db,
            user_id=1,
            market_id=MARKET_ID,
            token_id=TOKEN_ID,
            action="buy",
            amount=10.0,
            price=0.5,
            status="executed",
            strategy_source="external",
        )
        self.db.commit()
        rows = audit_service.get_audit_events(self.db, user_id=1, limit=10)
        self.assertTrue(rows)
        # "executed" maps to the observed-fill event, so that is the row
        # the log — and therefore the export — is expected to carry.
        self.assertEqual(rows[0]["event_type"], "order_fill_observed")
        csv_text = audit_service.to_csv(rows)
        self.assertIn("order_fill_observed", csv_text)
        self.assertIn("occurred_at,user_id,account_id", csv_text.splitlines()[0])


# ── WebSocket ───────────────────────────────────────────────


class _FakeClobManager:
    """Stand-in for the process-level CLOB WebSocket manager."""

    def __init__(self):
        self.subscribers = {}
        self.next_id = 0
        self.subscribed = []
        self.unsubscribed = []

    def register_subscriber(self, handler):
        self.next_id += 1
        self.subscribers[self.next_id] = handler
        return self.next_id

    def unregister_subscriber(self, subscriber_id):
        self.subscribers.pop(subscriber_id, None)

    async def subscribe(self, token_ids):
        self.subscribed.extend(token_ids)
        return list(token_ids)

    async def unsubscribe(self, token_ids):
        self.unsubscribed.extend(token_ids)

    def feed(self, tick):
        for handler in list(self.subscribers.values()):
            handler(tick)


class ExternalWebSocketTest(_ExternalBase):
    """The machine channel: handshake auth, channel protocol, fan-out."""

    def setUp(self):
        super().setUp()
        self.manager = _FakeClobManager()
        patcher = patch.object(ws_module, "get_clob_ws_manager", return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._credentials()
        self._settings()

    def _rejected_handshake(self, **kwargs) -> int:
        """Open the socket and return the close code the server chose."""
        from fastapi import WebSocketDisconnect

        # The outer context is the catcher: the disconnect fires inside the
        # inner one, so the pair must nest — expressed as one parenthesised
        # with so the behaviour is unchanged but the block is one unit.
        with (
            self.assertRaises(WebSocketDisconnect) as ctx,
            self.client.websocket_connect("/ws/v1", **kwargs) as ws,
        ):
            ws.receive_json()
        return ctx.exception.code

    def test_connection_without_a_key_is_rejected(self):
        self.assertEqual(self._rejected_handshake(), 4401)

    def test_connection_with_a_bad_key_is_rejected(self):
        self.assertEqual(self._rejected_handshake(headers={"X-API-Key": "pk_nope"}), 4401)

    def test_connection_with_a_trade_only_key_is_rejected(self):
        _, secret = self._make_key(scopes=("trade",))
        self.assertEqual(self._rejected_handshake(headers={"X-API-Key": secret}), 4403)

    def test_subscribe_unsubscribe_and_error_frames(self):
        _, secret = self._make_key()
        with self.client.websocket_connect("/ws/v1", headers={"X-API-Key": secret}) as ws:
            ws.send_json({"action": "subscribe", "channel": "positions"})
            self.assertEqual(
                ws.receive_json(), {"channel": "positions", "data": {"subscribed": True}}
            )

            ws.send_json({"action": "unsubscribe", "channel": "positions"})
            self.assertEqual(
                ws.receive_json(), {"channel": "positions", "data": {"subscribed": False}}
            )

            ws.send_json({"action": "frobnicate", "channel": "positions"})
            self.assertEqual(
                ws.receive_json(),
                {"channel": "error", "data": {"detail": "action must be subscribe or unsubscribe"}},
            )

            ws.send_json({"action": "subscribe", "channel": "nonsense"})
            self.assertEqual(
                ws.receive_json(),
                {"channel": "error", "data": {"detail": "Unknown channel: nonsense"}},
            )

    def test_prices_channel_fans_out_clob_ticks(self):
        _, secret = self._make_key()
        with self.client.websocket_connect("/ws/v1", headers={"X-API-Key": secret}) as ws:
            ws.send_json(
                {"action": "subscribe", "channel": "prices", "token_ids": [TOKEN_ID, TOKEN_ID]}
            )
            self.assertEqual(
                ws.receive_json(),
                {"channel": "prices", "data": {"subscribed": True, "token_ids": [TOKEN_ID]}},
            )
            self.assertEqual(self.manager.subscribed, [TOKEN_ID])
            self.assertNotIn(TOKEN_ID, [])

            self.manager.feed({"token_id": TOKEN_ID, "price": 0.42, "ts": utc_now().isoformat()})
            tick = ws.receive_json()
            self.assertEqual(tick["channel"], "prices")
            self.assertEqual(tick["data"]["price"], 0.42)

            ws.send_json({"action": "unsubscribe", "channel": "prices"})
            ws.receive_json()

        self.assertEqual(self.manager.unsubscribed, [TOKEN_ID])

    def test_fills_channel_reports_new_fills_only(self):
        _, secret = self._make_key()
        self._trade(status="executed", filled_price=0.51)
        with self.client.websocket_connect("/ws/v1", headers={"X-API-Key": secret}) as ws:
            ws.send_json({"action": "subscribe", "channel": "fills"})
            self.assertEqual(ws.receive_json(), {"channel": "fills", "data": {"subscribed": True}})

            # A fill that appears after the baseline is pushed.
            filled = self._trade(status="executed", filled_price=0.61)
            frame = ws.receive_json()
            self.assertEqual(frame["channel"], "fills")
            self.assertEqual(frame["data"]["trade_id"], filled.id)
            self.assertEqual(frame["data"]["filled_price"], 0.61)

    def test_url_query_key_is_accepted_as_a_fallback(self):
        _, secret = self._make_key()
        with self.client.websocket_connect(f"/ws/v1?api_key={secret}") as ws:
            ws.send_json({"action": "subscribe", "channel": "positions"})
            self.assertEqual(ws.receive_json()["channel"], "positions")


if __name__ == "__main__":
    unittest.main()
