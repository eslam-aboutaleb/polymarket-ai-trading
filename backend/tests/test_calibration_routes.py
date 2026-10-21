"""Route tests for app/api/routes/calibration.py (plan 01).

A dedicated ``FastAPI`` app mounts the calibration router (the router is
registered into the real app by the coordinator's main.py change), with
``get_current_user_from_token`` and ``get_db`` overridden and a real
in-memory SQLite session — same TestClient + dependency-override style
as test_portfolio_routes_coverage.py.
"""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base)
from app.api.routes.analysis.calibration import BASELINE_BRIER, router
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.analytics.assessment import Assessment
from app.models.base import Base
from app.models.trading.trade_history import TradeHistory
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.utils.database import get_db
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20
TRADE_HISTORY_ID = 77


def _make_app_and_db(is_admin=False, db=None):
    engine = None
    if db is None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
        db = session_factory()

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_current_user_from_token] = lambda: {
        "user_id": 1,
        "wallet_address": WALLET,
        "is_admin": is_admin,
    }
    test_app.dependency_overrides[get_db] = lambda: db
    return test_app, TestClient(test_app), db, engine


class CalibrationRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.app, self.client, self.db, self.engine = _make_app_and_db()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self._seed()

    def _seed(self):
        user = User(id=1, wallet_address=WALLET, is_admin=False)
        self.db.add(user)
        self.db.commit()

        trade = TradeHistory(
            id=TRADE_HISTORY_ID,
            market_id="market-1",
            wallet_address="0x" + "ef" * 20,
            order_type="buy",
            amount=25.0,
            price=0.5,
            timestamp=utc_now(),
        )
        self.db.add(trade)
        self.db.commit()

        self.db.add(
            UserTrade(
                user_id=1,
                market_id="market-1",
                action="buy",
                amount=25.0,
                price=0.5,
                status="executed",
                source_trade_history_id=TRADE_HISTORY_ID,
            ),
        )
        self.db.commit()


class SummaryTests(CalibrationRouteTestBase):
    def _verdict(self, **overrides):
        defaults = {
            "trade_history_id": TRADE_HISTORY_ID,
            "ai_score": 70,
            "reasoning": "r",
            "recommendation": "buy",
            "risk_level": "unknown",
            "confidence": 80,
            "ai_probability": 0.8,
            "market_price_at_assessment": 0.6,
            "model_name": "gpt-4",
            "provider": "openai",
            "task": "copy_trade_eval",
            "created_at": utc_now(),
        }
        defaults.update(overrides)
        row = Assessment(**defaults)
        self.db.add(row)
        self.db.commit()
        return row

    def test_empty_window_returns_baseline_only(self):
        response = self.client.get("/api/analysis/calibration/summary")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["sample_count"], 0)
        self.assertEqual(data["resolved_count"], 0)
        self.assertIsNone(data["mean_brier"])
        self.assertEqual(data["brier_vs_baseline"], BASELINE_BRIER)
        self.assertEqual(len(data["reliability"]), 10)
        self.assertEqual(data["by_model"], [])
        self.assertEqual(data["by_task"], [])
        self.assertEqual(data["data_quality"]["legacy_excluded"], 0)

    def test_user_scope_counts_only_linked_rows(self):
        self._verdict()
        # A scan verdict (no trade_history link) is NOT user-scoped.
        self._verdict(trade_history_id=None, task="opportunity_scan", ai_probability=0.3)
        response = self.client.get("/api/analysis/calibration/summary")
        data = response.json()
        self.assertEqual(data["sample_count"], 1)
        self.assertEqual(data["scope"], "user")

    def test_resolved_rows_feed_mean_brier_and_reliability(self):
        row = self._verdict(ai_probability=0.8, market_price_at_assessment=0.6)
        row.resolved_outcome = "YES"
        row.brier_score = 0.04
        row.resolved_at = utc_now()
        self.db.commit()
        response = self.client.get("/api/analysis/calibration/summary")
        data = response.json()
        self.assertEqual(data["resolved_count"], 1)
        self.assertAlmostEqual(data["mean_brier"], 0.04)
        bucket = next(b for b in data["reliability"] if b["count"] > 0)
        self.assertEqual(bucket["bucket"], "0.8-0.9")
        self.assertAlmostEqual(bucket["predicted"], 0.8)
        self.assertAlmostEqual(bucket["actual"], 1.0)
        self.assertEqual(bucket["count"], 1)

    def test_reliability_excludes_unverifiable_named_resolutions(self):
        """Historical nonbinary labels must not enter binary calibration."""
        row = self._verdict(ai_probability=0.8, market_price_at_assessment=0.6)
        row.resolved_outcome = "ALICE"
        row.brier_score = 0.64
        row.resolved_at = utc_now()
        self.db.commit()
        response = self.client.get("/api/analysis/calibration/summary")
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["resolved_count"], 0)
        self.assertIsNone(data["mean_brier"])
        self.assertEqual(sum(b["count"] for b in data["reliability"]), 0)
        self.assertEqual(data["data_quality"]["invalid_legacy_resolution"], 1)

    def test_by_model_and_by_task_stats(self):
        row = self._verdict(model_name="gpt-4", task="copy_trade_eval")
        row.resolved_outcome = "NO"
        row.brier_score = 0.64
        row.resolved_at = utc_now()
        self.db.commit()
        # Unattributed model (cli-agent blindness) is excluded from
        # by_model but still counted in the sample.
        self._verdict(model_name="unknown", task="opportunity_scan")
        response = self.client.get("/api/analysis/calibration/summary")
        data = response.json()
        self.assertEqual(data["sample_count"], 2)
        self.assertEqual(data["data_quality"]["missing_model"], 1)
        self.assertEqual(
            data["by_model"],
            [{"model_name": "gpt-4", "count": 1, "mean_brier": 0.64}],
        )
        by_task = {row["task"]: row for row in data["by_task"]}
        self.assertEqual(by_task["copy_trade_eval"]["mean_brier"], 0.64)
        self.assertIsNone(by_task["opportunity_scan"]["mean_brier"])

    def test_legacy_rows_are_excluded_not_backfilled(self):
        self._verdict()
        legacy = self._verdict(ai_probability=None)
        response = self.client.get("/api/analysis/calibration/summary")
        data = response.json()
        self.assertEqual(data["sample_count"], 1)
        self.assertEqual(data["data_quality"]["legacy_excluded"], 1)
        self.db.refresh(legacy)
        self.assertIsNone(legacy.ai_probability)

    def test_window_days_filters_old_rows(self):
        self._verdict()
        self._verdict(created_at=utc_now().replace(year=utc_now().year - 1))
        response = self.client.get("/api/analysis/calibration/summary", params={"window_days": 7})
        self.assertEqual(response.json()["sample_count"], 1)

    def test_global_scope_requires_admin(self):
        self._verdict()
        response = self.client.get("/api/analysis/calibration/summary", params={"scope": "global"})
        self.assertEqual(response.status_code, 403)

    def test_global_scope_for_admin_includes_scan_rows(self):
        self._verdict()
        self._verdict(trade_history_id=None, task="opportunity_scan")

        _, admin_client, _, _ = _make_app_and_db(is_admin=True, db=self.db)
        response = admin_client.get("/api/analysis/calibration/summary", params={"scope": "global"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sample_count"], 2)

    def test_requires_authentication(self):
        unauthenticated = FastAPI()
        unauthenticated.include_router(router)
        response = TestClient(unauthenticated).get("/api/analysis/calibration/summary")
        self.assertEqual(response.status_code, 401)


class ForecastsTests(CalibrationRouteTestBase):
    def _verdict(self, **overrides):
        defaults = {
            "trade_history_id": TRADE_HISTORY_ID,
            "ai_score": 70,
            "reasoning": "r",
            "recommendation": "buy",
            "risk_level": "unknown",
            "confidence": 80,
            "ai_probability": 0.8,
            "market_price_at_assessment": 0.6,
            "model_name": "gpt-4",
            "provider": "openai",
            "task": "copy_trade_eval",
            "created_at": utc_now(),
        }
        defaults.update(overrides)
        row = Assessment(**defaults)
        self.db.add(row)
        self.db.commit()
        return row

    def test_forecast_rows_expose_edge_and_resolution(self):
        row = self._verdict(ai_probability=0.8, market_price_at_assessment=0.6)
        row.resolved_outcome = "YES"
        row.brier_score = 0.04
        row.resolved_at = utc_now()
        self.db.commit()
        response = self.client.get("/api/analysis/calibration/forecasts")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        forecast = data["forecasts"][0]
        self.assertEqual(forecast["task"], "copy_trade_eval")
        self.assertEqual(forecast["model"], "gpt-4")
        self.assertAlmostEqual(forecast["ai_probability"], 0.8)
        self.assertAlmostEqual(forecast["market_price"], 0.6)
        self.assertAlmostEqual(forecast["edge"], 0.2)
        self.assertEqual(forecast["resolved_outcome"], "YES")
        self.assertAlmostEqual(forecast["brier_score"], 0.04)
        self.assertIsNotNone(forecast["created_at"])

    def test_limit_caps_rows(self):
        for _i in range(3):
            self._verdict()
        response = self.client.get("/api/analysis/calibration/forecasts", params={"limit": 2})
        self.assertEqual(response.json()["count"], 2)

    def test_global_scope_requires_admin(self):
        self._verdict()
        response = self.client.get(
            "/api/analysis/calibration/forecasts", params={"scope": "global"}
        )
        self.assertEqual(response.status_code, 403)

    def test_requires_authentication(self):
        unauthenticated = FastAPI()
        unauthenticated.include_router(router)
        response = TestClient(unauthenticated).get("/api/analysis/calibration/forecasts")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
