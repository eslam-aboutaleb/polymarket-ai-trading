"""Coverage for the latency-arbitrage routes (``/api/latency-arb``).

Exercises config read/update (including symbol and window
filtering against the engine catalog), the live opportunities
board, the paginated trade log, request validation, and the
response-shaping helpers — with the service layer and the DB
session mocked or backed by an in-memory SQLite database.
"""

import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.latency_arb import (
    _config_to_response,
    _opportunity_to_response,
)
from app.main import app
from app.models.base import Base
from app.models.user_trade import UserTrade
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _fake_config(**overrides):
    base = {
        "enabled": False,
        "edge_threshold": 0.03,
        "max_notional": 50.0,
        "symbols": ["BTC"],
        "windows": [5],
        "late_entry": False,
        "daily_loss_limit": 20.0,
        "alert_on_opportunity": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_opportunity(**overrides):
    base = {
        "symbol": "BTC",
        "window_minutes": 5,
        "window_start_epoch": 1000,
        "window_end_epoch": int(time.time()) + 60,
        "side": "buy",
        "p_model": 0.6,
        "p_market": 0.5,
        "edge": 0.1,
        "distance": 100.0,
        "t_remaining": 30.0,
        "sigma": 0.05,
        "current_price": 100.0,
        "window_open": 90.0,
        "condition_id": "0xcond",
        "question": "Will BTC go up?",
        "token_ids": {"YES": "t1"},
        "prices": {"YES": 0.5},
        "feed_lag_ms": 12.5,
        "detected_at": "2026-01-01T00:00:00",
    }
    base.update(overrides)
    return base


class LatencyArbRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()
        app.dependency_overrides[get_db] = lambda: self.session
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def _authenticate(self, user_id=1):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": user_id,
            "wallet_address": WALLET,
            "is_admin": False,
        }


class ConfigRouteTests(LatencyArbRouteTestCase):
    def test_get_config_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/latency-arb/config")
        self.assertEqual(response.status_code, 401)

    def test_get_config_returns_current_values(self):
        config = _fake_config(
            enabled=True,
            edge_threshold=0.05,
            symbols=["BTC", "ETH"],
            windows=[5, 15],
        )
        with patch("app.api.routes.latency_arb.get_config", return_value=config):
            response = self.client.get("/api/latency-arb/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["edge_threshold"], 0.05)
        self.assertEqual(body["symbols"], ["BTC", "ETH"])
        self.assertEqual(body["windows"], [5, 15])
        self.assertEqual(body["max_notional"], 50.0)
        self.assertFalse(body["late_entry"])
        self.assertEqual(body["daily_loss_limit"], 20.0)
        self.assertTrue(body["alert_on_opportunity"])

    def test_post_config_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.post("/api/latency-arb/config", json={})
        self.assertEqual(response.status_code, 401)

    def test_post_config_updates_all_fields(self):
        fake_db = MagicMock()
        app.dependency_overrides[get_db] = lambda: fake_db
        config = _fake_config()

        def _get_or_create(user_id, db):
            return config

        with patch(
            "app.api.routes.latency_arb.get_or_create_config",
            side_effect=_get_or_create,
        ):
            response = self.client.post(
                "/api/latency-arb/config",
                json={
                    "enabled": True,
                    "edge_threshold": 0.05,
                    "max_notional": 100.0,
                    "late_entry": True,
                    "daily_loss_limit": 0.0,
                    "alert_on_opportunity": False,
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["edge_threshold"], 0.05)
        self.assertEqual(body["max_notional"], 100.0)
        self.assertTrue(body["late_entry"])
        self.assertEqual(body["daily_loss_limit"], 0.0)
        self.assertFalse(body["alert_on_opportunity"])

    def test_post_config_filters_symbols_and_windows(self):
        fake_db = MagicMock()
        app.dependency_overrides[get_db] = lambda: fake_db
        config = _fake_config()

        def _get_or_create(user_id, db):
            return config

        with (
            patch(
                "app.api.routes.latency_arb.get_or_create_config",
                side_effect=_get_or_create,
            ),
            patch(
                "app.services.crypto_markets_service.SYMBOLS",
                ("BTC", "ETH", "SOL"),
            ),
            patch(
                "app.services.crypto_markets_service.WINDOW_MINUTES",
                (5, 15, 60),
            ),
        ):
            response = self.client.post(
                "/api/latency-arb/config",
                json={
                    "symbols": ["btc", "ETH", "nope", ""],
                    "windows": [15, 99],
                },
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["symbols"], ["BTC", "ETH"])
        self.assertEqual(body["windows"], [15])

    def test_post_config_rejects_invalid_thresholds(self):
        for body in (
            {"edge_threshold": 0.0},
            {"edge_threshold": 1.0},
            {"max_notional": 0.0},
            {"daily_loss_limit": -1.0},
        ):
            response = self.client.post("/api/latency-arb/config", json=body)
            self.assertEqual(response.status_code, 422, body)


class OpportunitiesRouteTests(LatencyArbRouteTestCase):
    def test_opportunities_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/latency-arb/opportunities")
        self.assertEqual(response.status_code, 401)

    def test_opportunities_returns_engine_latency_and_board(self):
        opportunity = _fake_opportunity()
        engine_status = {
            "running": True,
            "live_mode": False,
            "last_cycle_at": "2026-01-01T00:00:00",
            "cycle_seconds": 30,
            "symbols": ["BTC"],
            "windows": [5],
        }
        stats = {
            "samples": 10,
            "feed_lag_p50_ms": 12.0,
            "feed_lag_p95_ms": 40.0,
            "total_p50_ms": 50.0,
            "total_p95_ms": 90.0,
        }
        with (
            patch(
                "app.api.routes.latency_arb.get_latest_opportunities",
                return_value=[opportunity],
            ),
            patch(
                "app.api.routes.latency_arb.get_engine_status",
                return_value=engine_status,
            ),
            patch(
                "app.api.routes.latency_arb.latency_stats",
                return_value=stats,
            ),
        ):
            response = self.client.get("/api/latency-arb/opportunities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["engine"]["running"])
        self.assertEqual(body["latency"]["samples"], 10)
        self.assertEqual(len(body["opportunities"]), 1)
        item = body["opportunities"][0]
        self.assertEqual(item["symbol"], "BTC")
        self.assertEqual(item["side"], "buy")
        self.assertEqual(item["edge"], 0.1)
        self.assertEqual(item["token_ids"], {"YES": "t1"})
        self.assertEqual(item["feed_lag_ms"], 12.5)
        self.assertGreater(item["seconds_remaining"], 0)

    def test_opportunities_with_empty_board(self):
        with (
            patch(
                "app.api.routes.latency_arb.get_latest_opportunities",
                return_value=[],
            ),
            patch(
                "app.api.routes.latency_arb.get_engine_status",
                return_value={
                    "running": False,
                    "live_mode": False,
                    "last_cycle_at": None,
                    "cycle_seconds": 30,
                    "symbols": [],
                    "windows": [],
                },
            ),
            patch(
                "app.api.routes.latency_arb.latency_stats",
                return_value={
                    "samples": 0,
                    "feed_lag_p50_ms": 0.0,
                    "feed_lag_p95_ms": 0.0,
                    "total_p50_ms": 0.0,
                    "total_p95_ms": 0.0,
                },
            ),
        ):
            response = self.client.get("/api/latency-arb/opportunities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["opportunities"], [])
        self.assertFalse(body["engine"]["running"])


class TradesRouteTests(LatencyArbRouteTestCase):
    def _add_trade(self, user_id, market_id, strategy, minutes_ago=0):
        trade = UserTrade(
            user_id=user_id,
            market_id=market_id,
            token_id="t1",
            action="buy",
            amount=10.0,
            price=0.5,
            status="executed",
            strategy_source=strategy,
            created_at=datetime.now() - timedelta(minutes=minutes_ago),
        )
        self.session.add(trade)
        self.session.commit()
        self.session.refresh(trade)
        return trade

    def test_trades_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/latency-arb/trades")
        self.assertEqual(response.status_code, 401)

    def test_trades_are_scoped_to_user_and_strategy(self):
        self._add_trade(1, "m1", "latency_arb", minutes_ago=2)
        self._add_trade(1, "m2", "latency_arb", minutes_ago=1)
        self._add_trade(1, "m3", "copy_trading", minutes_ago=0)
        self._add_trade(2, "m4", "latency_arb", minutes_ago=0)
        response = self.client.get("/api/latency-arb/trades")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["limit"], 50)
        self.assertEqual(body["offset"], 0)
        # Newest first, and only the current user's latency-arb trades.
        self.assertEqual(body["trades"][0]["market_id"], "m2")
        self.assertEqual(body["trades"][1]["market_id"], "m1")
        for trade in body["trades"]:
            self.assertEqual(trade["strategy_source"], "latency_arb")

    def test_trades_pagination(self):
        for index in range(5):
            self._add_trade(1, f"m{index}", "latency_arb", minutes_ago=index)
        response = self.client.get("/api/latency-arb/trades?limit=2&offset=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["offset"], 1)
        self.assertEqual(len(body["trades"]), 2)
        self.assertEqual(body["trades"][0]["market_id"], "m1")
        self.assertEqual(body["trades"][1]["market_id"], "m2")

    def test_trades_validation_errors(self):
        for query in ("limit=0", "limit=501", "offset=-1"):
            response = self.client.get(f"/api/latency-arb/trades?{query}")
            self.assertEqual(response.status_code, 422, query)

    def test_trades_empty_log(self):
        response = self.client.get("/api/latency-arb/trades")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["trades"], [])
        self.assertEqual(body["total"], 0)


class ResponseHelperTests(unittest.TestCase):
    def test_config_to_response_with_none_collections(self):
        config = _fake_config(symbols=None, windows=None)
        response = _config_to_response(config)
        self.assertEqual(response.symbols, [])
        self.assertEqual(response.windows, [])

    def test_opportunity_to_response_with_empty_dict(self):
        response = _opportunity_to_response({}, datetime.now())
        self.assertEqual(response.symbol, "")
        self.assertEqual(response.window_minutes, 0)
        self.assertEqual(response.window_end_epoch, 0)
        self.assertEqual(response.seconds_remaining, 0)
        self.assertEqual(response.token_ids, {})
        self.assertEqual(response.prices, {})
        self.assertIsNone(response.feed_lag_ms)

    def test_opportunity_to_response_computes_seconds_remaining(self):
        now = datetime.now()
        opportunity = _fake_opportunity(window_end_epoch=int(now.timestamp()) + 120)
        response = _opportunity_to_response(opportunity, now)
        self.assertGreaterEqual(response.seconds_remaining, 119)

    def test_opportunity_to_response_clamps_past_windows(self):
        opportunity = _fake_opportunity(window_end_epoch=0)
        response = _opportunity_to_response(opportunity, datetime.now())
        self.assertEqual(response.seconds_remaining, 0)


if __name__ == "__main__":
    unittest.main()
