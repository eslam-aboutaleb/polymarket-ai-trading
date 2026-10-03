"""Coverage for the backtesting API routes
(:mod:`app.api.routes.backtesting`).

Exercises every endpoint through ``TestClient`` with the auth
dependency overridden: listing (with the 100-row limit clamp),
single-run retrieval (200 / 404), creation (200 / 400 / 500),
deletion (200 / 404), the strategy catalogue, request validation,
and the ``_to_response`` mapping including its None-field defaults.
"""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.backtesting import _to_response
from app.main import app
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20


def _run(**overrides):
    """Build a BacktestRun-like object with sensible defaults."""
    fields = {
        "id": 1,
        "user_id": 1,
        "strategy_type": "copy_trade",
        "strategy_name": "Copy Trade",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "parameters": {"followed_wallet": WALLET},
        "total_trades": 10,
        "winning_trades": 6,
        "losing_trades": 4,
        "win_rate": 60.0,
        "total_pnl": 12.5,
        "max_drawdown": 3.2,
        "sharpe_ratio": 1.1,
        "profit_factor": 2.0,
        "avg_trade_pnl": 1.25,
        "max_consecutive_losses": 2,
        "total_volume": 100.0,
        "status": "completed",
        "error_message": None,
        "trade_log": [],
        "indicator_values": {},
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "completed_at": datetime(2024, 1, 2, tzinfo=UTC),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class ToResponseTests(unittest.TestCase):
    """``_to_response`` field mapping and defaults."""

    def test_maps_all_fields(self):
        response = _to_response(_run())
        self.assertEqual(response.id, 1)
        self.assertEqual(response.strategy_type, "copy_trade")
        self.assertEqual(response.strategy_name, "Copy Trade")
        self.assertEqual(response.start_date, "2024-01-01")
        self.assertEqual(response.end_date, "2024-01-31")
        self.assertEqual(response.parameters, {"followed_wallet": WALLET})
        self.assertEqual(response.total_trades, 10)
        self.assertEqual(response.winning_trades, 6)
        self.assertEqual(response.losing_trades, 4)
        self.assertEqual(response.win_rate, 60.0)
        self.assertEqual(response.total_pnl, 12.5)
        self.assertEqual(response.max_drawdown, 3.2)
        self.assertEqual(response.sharpe_ratio, 1.1)
        self.assertEqual(response.profit_factor, 2.0)
        self.assertEqual(response.avg_trade_pnl, 1.25)
        self.assertEqual(response.max_consecutive_losses, 2)
        self.assertEqual(response.total_volume, 100.0)
        self.assertEqual(response.status, "completed")
        self.assertIsNone(response.error_message)
        self.assertEqual(response.trade_log, [])
        self.assertEqual(response.indicator_values, {})
        self.assertEqual(response.created_at, "2024-01-01T00:00:00+00:00")
        self.assertEqual(response.completed_at, "2024-01-02T00:00:00+00:00")

    def test_none_and_empty_fields_use_defaults(self):
        response = _to_response(
            _run(
                strategy_name=None,
                parameters=None,
                total_trades=None,
                winning_trades=None,
                losing_trades=None,
                total_pnl=None,
                status=None,
                created_at=None,
                completed_at=None,
            )
        )
        self.assertEqual(response.strategy_name, "")
        self.assertEqual(response.parameters, {})
        self.assertEqual(response.total_trades, 0)
        self.assertEqual(response.winning_trades, 0)
        self.assertEqual(response.losing_trades, 0)
        self.assertEqual(response.total_pnl, 0.0)
        self.assertEqual(response.status, "pending")
        self.assertEqual(response.created_at, "")
        self.assertIsNone(response.completed_at)


class BacktestingRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        self.addCleanup(app.dependency_overrides.clear)

    # ── GET /api/backtesting/runs ─────────────────────

    def test_list_runs_returns_runs_and_total(self):
        run = _run(id=7)
        with patch(
            "app.api.routes.backtesting.get_backtest_runs",
            return_value=[run],
        ) as list_mock:
            response = self.client.get("/api/backtesting/runs")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["runs"][0]["id"], 7)
        self.assertEqual(body["runs"][0]["strategy_name"], "Copy Trade")
        list_mock.assert_called_once_with(1, limit=20)

    def test_list_runs_clamps_limit_to_one_hundred(self):
        with patch(
            "app.api.routes.backtesting.get_backtest_runs",
            return_value=[],
        ) as list_mock:
            response = self.client.get("/api/backtesting/runs?limit=500")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)
        list_mock.assert_called_once_with(1, limit=100)

    def test_list_runs_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/backtesting/runs")
        self.assertEqual(response.status_code, 401)

    # ── GET /api/backtesting/runs/{run_id} ────────────

    def test_get_run_returns_run(self):
        run = _run(id=9)
        with patch(
            "app.api.routes.backtesting.get_backtest_run",
            return_value=run,
        ) as get_mock:
            response = self.client.get("/api/backtesting/runs/9")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], 9)
        get_mock.assert_called_once_with(9, 1)

    def test_get_run_returns_404_when_missing(self):
        with patch(
            "app.api.routes.backtesting.get_backtest_run",
            return_value=None,
        ):
            response = self.client.get("/api/backtesting/runs/999")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Backtest run not found")

    # ── POST /api/backtesting/runs ────────────────────

    def test_create_run_returns_created_run(self):
        run = _run(id=11, status="completed")
        with patch(
            "app.api.routes.backtesting.create_and_run_backtest",
            new=AsyncMock(return_value=run),
        ) as create_mock:
            response = self.client.post(
                "/api/backtesting/runs",
                json={
                    "strategy_type": "copy_trade",
                    "strategy_name": "My Strategy",
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                    "parameters": {"followed_wallet": WALLET},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], 11)
        create_mock.assert_awaited_once_with(
            user_id=1,
            strategy_type="copy_trade",
            strategy_name="My Strategy",
            start_date="2024-01-01",
            end_date="2024-01-31",
            parameters={"followed_wallet": WALLET},
        )

    def test_create_run_returns_400_on_value_error(self):
        with patch(
            "app.api.routes.backtesting.create_and_run_backtest",
            new=AsyncMock(side_effect=ValueError("start_date must be on or before end_date")),
        ):
            response = self.client.post(
                "/api/backtesting/runs",
                json={
                    "strategy_type": "indicator",
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "start_date must be on or before end_date")

    def test_create_run_returns_500_when_service_returns_none(self):
        with patch(
            "app.api.routes.backtesting.create_and_run_backtest",
            new=AsyncMock(return_value=None),
        ):
            response = self.client.post(
                "/api/backtesting/runs",
                json={
                    "strategy_type": "custom",
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Failed to create backtest run")

    def test_create_run_rejects_unknown_strategy_type(self):
        response = self.client.post(
            "/api/backtesting/runs",
            json={
                "strategy_type": "bogus",
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_create_run_rejects_short_dates(self):
        response = self.client.post(
            "/api/backtesting/runs",
            json={
                "strategy_type": "copy_trade",
                "start_date": "2024-1",
                "end_date": "2024-01-31",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_create_run_defaults_strategy_name_and_parameters(self):
        run = _run(id=12, strategy_name="Unnamed Strategy", parameters={})
        with patch(
            "app.api.routes.backtesting.create_and_run_backtest",
            new=AsyncMock(return_value=run),
        ) as create_mock:
            response = self.client.post(
                "/api/backtesting/runs",
                json={
                    "strategy_type": "indicator",
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                },
            )

        self.assertEqual(response.status_code, 200)
        create_mock.assert_awaited_once_with(
            user_id=1,
            strategy_type="indicator",
            strategy_name="Unnamed Strategy",
            start_date="2024-01-01",
            end_date="2024-01-31",
            parameters={},
        )

    # ── DELETE /api/backtesting/runs/{run_id} ─────────

    def test_delete_run_deletes_and_commits(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = _run(id=5)
        app.dependency_overrides[get_db] = lambda: db

        response = self.client.delete("/api/backtesting/runs/5")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "deleted", "id": 5})
        db.delete.assert_called_once()
        self.assertEqual(db.delete.call_args.args[0].id, 5)
        db.commit.assert_called_once_with()

    def test_delete_run_returns_404_when_missing(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        app.dependency_overrides[get_db] = lambda: db

        response = self.client.delete("/api/backtesting/runs/5")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Backtest run not found")
        db.delete.assert_not_called()
        db.commit.assert_not_called()

    # ── GET /api/backtesting/strategies ───────────────

    def test_list_available_strategies(self):
        response = self.client.get("/api/backtesting/strategies")

        self.assertEqual(response.status_code, 200)
        strategies = response.json()["strategies"]
        self.assertEqual(len(strategies), 2)
        types = [s["type"] for s in strategies]
        self.assertEqual(types, ["copy_trade", "indicator"])
        for strategy in strategies:
            self.assertIn("name", strategy)
            self.assertIn("description", strategy)
            self.assertIn("parameters", strategy)


if __name__ == "__main__":
    unittest.main()
