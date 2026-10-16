"""Route tests for ``GET /api/portfolio/risk-report``.

Uses the same in-memory SQLite + dependency-override harness as
``test_portfolio_routes_coverage.py``: the Polymarket service, the credential
store and the analysis client are mocked while the ``UserSettings`` lookup runs
against a real session. Covers the happy path, token auth, the
degrade-never-500 contract when the LLM is down, paper-mode labelling and the
credential-store failure path.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.routes.portfolio.risk_report as risk_report_module
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.user import User
from app.models.user_settings import UserSettings
from app.security.credential_store import CredentialStoreError
from app.services.analytics import portfolio_risk_service
from app.utils.cache import InMemoryCache
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20
PRIVATE_KEY = "0x" + "11" * 32
CLOB_CREDS = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}

# The router is exercised on a minimal app so these tests pass with or without
# the coordinator's ``app.include_router`` in app/main.py.
risk_test_app = FastAPI()
risk_test_app.include_router(risk_report_module.router)

POSITIONS = [
    {
        "title": "Will the Fed cut rates in March?",
        "condition_id": "0xfed",
        "outcome": "Yes",
        "size": 100,
        "avgPrice": 0.3,
        "curPrice": 0.5,
        "pnl": 20,
    },
    {
        "title": "Will Bitcoin close above 100k?",
        "condition_id": "0xbtc",
        "outcome": "No",
        "size": 20,
        "avgPrice": 0.5,
        "curPrice": 0.4,
        "pnl": -2,
    },
]


class RiskReportRouteTestBase(unittest.TestCase):
    """Shared fixtures: in-memory DB, auth override, mocked service + client."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        User.__table__.create(self.engine)
        UserSettings.__table__.create(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )
        self.db = self.session_factory()

        risk_test_app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        risk_test_app.dependency_overrides[get_db] = self._get_db_override
        self.client = TestClient(risk_test_app)
        self.addCleanup(self._cleanup)

        self.service = MagicMock()
        self.service.get_wallet_balance = AsyncMock(return_value={"usdc_balance": 100.0})
        self.service.get_positions = AsyncMock(return_value=[dict(pos) for pos in POSITIONS])

        self.analysis_client = MagicMock()
        self.analysis_client.generate_risk_report = AsyncMock(
            return_value={
                "report": "1. Concentration risks\n- Fed is half the book",
                "success": True,
            }
        )

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _cleanup(self):
        risk_test_app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def _settings(self, **fields) -> UserSettings:
        settings = UserSettings(user_id=1)
        for key, value in fields.items():
            setattr(settings, key, value)
        self.db.add(settings)
        self.db.commit()
        return settings

    def _get(self):
        """Call the endpoint with every external dependency mocked out."""
        with (
            patch.object(risk_report_module, "get_polymarket_service", return_value=self.service),
            patch.object(
                risk_report_module,
                "load_wallet_credentials",
                return_value={"private_key": PRIVATE_KEY, "clob_creds": CLOB_CREDS},
            ),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
            patch.object(
                portfolio_risk_service,
                "narrative_cache",
                InMemoryCache("test_risk_report_routes"),
            ),
            patch(
                "app.grpc_clients.analysis_client.get_analysis_client",
                return_value=self.analysis_client,
            ),
        ):
            return self.client.get("/api/portfolio/risk-report")


class RiskReportRouteTests(RiskReportRouteTestBase):
    def test_report_returns_metrics_and_narrative(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        # 100 USDC + (100 * 0.5) + (20 * 0.4) — Kelly's portfolio-value definition
        self.assertEqual(data["metrics"]["portfolio_value"], 158.0)
        self.assertEqual(data["metrics"]["positions_count"], 2)
        self.assertEqual(data["metrics"]["loss_bound"], 40.0)
        self.assertEqual(data["metrics"]["unrealized_pnl"], 18.0)
        self.assertEqual(data["metrics"]["concentration_flags"], 1)
        self.assertEqual(data["metrics"]["max_position_weight"], 0.316456)
        self.assertEqual(data["narrative"]["concentration_flags"], ["Fed is half the book"])
        self.assertTrue(data["generated_at"])

    def test_credentials_are_passed_to_the_authenticated_path(self):
        self._get()
        kwargs = self.service.get_positions.await_args.kwargs
        self.assertEqual(kwargs["private_key"], PRIVATE_KEY)
        self.assertEqual(kwargs["clob_creds"], CLOB_CREDS)

    def test_credential_store_failure_falls_back_to_the_public_path(self):
        with (
            patch.object(risk_report_module, "get_polymarket_service", return_value=self.service),
            patch.object(
                risk_report_module,
                "load_wallet_credentials",
                side_effect=CredentialStoreError("locked"),
            ),
            patch.object(
                portfolio_risk_service,
                "fetch_market_end_dates",
                AsyncMock(return_value={}),
            ),
            patch.object(
                portfolio_risk_service,
                "narrative_cache",
                InMemoryCache("test_risk_report_routes"),
            ),
            patch(
                "app.grpc_clients.analysis_client.get_analysis_client",
                return_value=self.analysis_client,
            ),
        ):
            response = self.client.get("/api/portfolio/risk-report")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.service.get_positions.await_args.kwargs["private_key"])

    def test_llm_failure_still_returns_metrics(self):
        self.analysis_client.generate_risk_report = AsyncMock(
            side_effect=RuntimeError("grpc unavailable")
        )
        response = self._get()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["narrative"])
        self.assertEqual(data["metrics"]["portfolio_value"], 158.0)
        self.assertEqual(data["metrics"]["positions_count"], 2)

    def test_llm_failure_flag_still_returns_metrics(self):
        self.analysis_client.generate_risk_report = AsyncMock(
            return_value={"report": "", "success": False}
        )
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["narrative"])
        self.assertEqual(response.json()["metrics"]["positions_count"], 2)

    def test_empty_portfolio_reports_zero_positions(self):
        self.service.get_positions = AsyncMock(return_value=[])
        response = self._get()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["metrics"]["positions_count"], 0)
        self.assertEqual(data["metrics"]["portfolio_value"], 100.0)
        self.assertEqual(data["metrics"]["clusters"], [])
        self.assertIsNone(data["narrative"])

    def test_wallet_failure_degrades_to_zeroes(self):
        self.service.get_wallet_balance = AsyncMock(side_effect=RuntimeError("rpc down"))
        self.service.get_positions = AsyncMock(side_effect=RuntimeError("rpc down"))
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"]["portfolio_value"], 0.0)

    def test_report_requires_auth(self):
        risk_test_app.dependency_overrides.pop(get_current_user_from_token, None)
        try:
            response = self.client.get("/api/portfolio/risk-report")
        finally:
            risk_test_app.dependency_overrides[get_current_user_from_token] = lambda: {
                "user_id": 1,
                "wallet_address": WALLET,
                "is_admin": False,
            }
        self.assertEqual(response.status_code, 401)


class RiskReportSettingsTests(RiskReportRouteTestBase):
    def test_live_risk_limits_are_echoed(self):
        self._settings(max_drawdown_pct=25.0, daily_loss_limit=500.0, monthly_loss_limit=250.0)
        risk_limits = self._get().json()["metrics"]["risk_limits"]

        self.assertEqual(risk_limits["max_drawdown_pct"], 25.0)
        self.assertEqual(risk_limits["daily_loss_limit"], 500.0)
        self.assertEqual(risk_limits["monthly_loss_limit"], 250.0)
        self.assertEqual(risk_limits["mode"], "live")

    def test_paper_mode_is_labelled(self):
        self._settings(simulation_mode=True, paper_balance=750.0)
        risk_limits = self._get().json()["metrics"]["risk_limits"]

        self.assertEqual(risk_limits["mode"], "paper")
        self.assertTrue(risk_limits["simulation_mode"])
        self.assertEqual(risk_limits["paper_balance"], 750.0)

    def test_halted_trading_is_visible(self):
        self._settings(trading_halted=True, halt_reason="Max drawdown 25% breached")
        risk_limits = self._get().json()["metrics"]["risk_limits"]

        self.assertTrue(risk_limits["trading_halted"])
        self.assertEqual(risk_limits["halt_reason"], "Max drawdown 25% breached")

    def test_missing_settings_row_yields_no_limits(self):
        risk_limits = self._get().json()["metrics"]["risk_limits"]

        self.assertEqual(risk_limits, {})


if __name__ == "__main__":
    unittest.main()
