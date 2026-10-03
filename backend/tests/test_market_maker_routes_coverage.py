"""Coverage tests for the market maker API routes (``/api/market-maker``).

Every endpoint is driven through ``TestClient`` with the auth and
DB dependencies overridden. The market-maker service functions
(``start_market_maker``, ``stop_market_maker``, ``trigger_single_sync``,
``get_running_maker_ids``) are replaced with mocks in the route
module's namespace, and the SQLAlchemy session is replaced with a
scripted fake so the routes' query chains, commits and response
shapes are asserted without a database.

Covered: list/create/update/delete configs, start/stop/sync
endpoints (including 404s and the already-running / not-running
branches), the metrics endpoint, and request-validation failures.
"""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.market_maker import (
    MarketMakerConfigRequest,
    _to_response,
)
from app.main import app
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20

CONFIG_PATH = "app.api.routes.market_maker"


def _config_row(**overrides) -> SimpleNamespace:
    """A MarketMakerConfig stand-in with every response field."""
    row = {
        "id": 1,
        "condition_id": "cond-1",
        "token_id_yes": "token-yes",
        "token_id_no": "token-no",
        "market_title": "Bitcoin 5m Up or Down",
        "enabled": True,
        "strategy": "bands",
        "num_bands": 3,
        "min_spread": 0.02,
        "max_spread": 0.10,
        "band_order_size": 10.0,
        "amm_liquidity": 1000.0,
        "max_collateral": 500.0,
        "sync_interval_seconds": 30,
        "min_order_size": 1.0,
        "min_price": 0.01,
        "max_price": 0.99,
        "status": "idle",
        "last_sync_at": None,
        "last_error": None,
        "total_orders_placed": 0,
        "total_orders_cancelled": 0,
        "total_volume_usdc": 0.0,
        "current_open_orders": 0,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": None,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


class _FakeSession:
    """Scripted stand-in for a SQLAlchemy session.

    ``query(...)`` returns the session itself so the routes'
    ``query(...).filter(...).order_by(...).all()`` and
    ``query(...).filter(...).first()`` chains resolve to the
    scripted rows. ``add`` assigns a primary key, mirroring the
    INSERT the real session would perform.
    """

    def __init__(self, first=None, all_rows=None) -> None:
        self._first = first
        self._all = list(all_rows or [])
        self.added: list = []
        self.refreshed: list = []
        self.commits = 0

    def query(self, *_args, **_kwargs) -> "_FakeSession":
        return self

    def filter(self, *_args, **_kwargs) -> "_FakeSession":
        return self

    def order_by(self, *_args, **_kwargs) -> "_FakeSession":
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all

    def add(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = 42
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, obj) -> None:
        self.refreshed.append(obj)

    def close(self) -> None:
        pass


class MarketMakerRouteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        self.addCleanup(app.dependency_overrides.clear)

        self.service_patchers = [
            patch(f"{CONFIG_PATH}.get_running_maker_ids", MagicMock(return_value=[])),
            patch(f"{CONFIG_PATH}.start_market_maker", new=AsyncMock(return_value=True)),
            patch(f"{CONFIG_PATH}.stop_market_maker", new=AsyncMock(return_value=True)),
            patch(f"{CONFIG_PATH}.trigger_single_sync", new=AsyncMock(return_value={})),
        ]
        self.mocks = {
            "get_running_maker_ids": self.service_patchers[0].start(),
            "start_market_maker": self.service_patchers[1].start(),
            "stop_market_maker": self.service_patchers[2].start(),
            "trigger_single_sync": self.service_patchers[3].start(),
        }
        for patcher in self.service_patchers:
            self.addCleanup(patcher.stop)

    def _override_db(self, session: _FakeSession) -> None:
        app.dependency_overrides[get_db] = lambda: session

    def _payload(self, **overrides) -> dict:
        payload = {
            "condition_id": "cond-1",
            "token_id_yes": "token-yes",
            "token_id_no": "token-no",
        }
        payload.update(overrides)
        return payload


class ListConfigsTests(MarketMakerRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/market-maker/configs")
        self.assertEqual(response.status_code, 401)

    def test_lists_configs_newest_first(self):
        rows = [_config_row(id=1), _config_row(id=2)]
        session = _FakeSession(all_rows=rows)
        self._override_db(session)

        response = self.client.get("/api/market-maker/configs")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([row["id"] for row in body], [1, 2])
        self.assertEqual(body[0]["condition_id"], "cond-1")
        self.assertEqual(body[0]["market_title"], "Bitcoin 5m Up or Down")
        self.assertEqual(body[0]["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertIsNone(body[0]["updated_at"])
        self.assertIsNone(body[0]["last_sync_at"])
        self.assertFalse(body[0]["is_running"])

    def test_running_configs_are_flagged(self):
        self.mocks["get_running_maker_ids"].return_value = [2]
        rows = [_config_row(id=1), _config_row(id=2)]
        self._override_db(_FakeSession(all_rows=rows))

        response = self.client.get("/api/market-maker/configs")

        self.assertEqual([row["is_running"] for row in response.json()], [False, True])

    def test_empty_list(self):
        self._override_db(_FakeSession(all_rows=[]))
        response = self.client.get("/api/market-maker/configs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_response_defaults_for_nullable_columns(self):
        row = _config_row(
            market_title=None,
            strategy=None,
            status=None,
            total_orders_placed=None,
            total_orders_cancelled=None,
            total_volume_usdc=None,
            current_open_orders=None,
            created_at=None,
        )
        self._override_db(_FakeSession(all_rows=[row]))

        body = self.client.get("/api/market-maker/configs").json()[0]

        self.assertEqual(body["market_title"], "")
        self.assertEqual(body["strategy"], "bands")
        self.assertEqual(body["status"], "idle")
        self.assertEqual(body["total_orders_placed"], 0)
        self.assertEqual(body["total_orders_cancelled"], 0)
        self.assertEqual(body["total_volume_usdc"], 0.0)
        self.assertEqual(body["current_open_orders"], 0)
        self.assertEqual(body["created_at"], "")

    def test_last_sync_at_is_serialised(self):
        row = _config_row(
            last_sync_at=datetime(2026, 2, 2, tzinfo=UTC),
            last_error="boom",
        )
        self._override_db(_FakeSession(all_rows=[row]))

        body = self.client.get("/api/market-maker/configs").json()[0]

        self.assertEqual(body["last_sync_at"], "2026-02-02T00:00:00+00:00")
        self.assertEqual(body["last_error"], "boom")


class UpsertConfigTests(MarketMakerRouteTestCase):
    def test_creates_a_new_config(self):
        session = _FakeSession(first=None)
        self._override_db(session)

        response = self.client.post("/api/market-maker/configs", json=self._payload())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], 42)
        self.assertEqual(body["condition_id"], "cond-1")
        self.assertEqual(body["enabled"], False)
        self.assertEqual(body["strategy"], "bands")
        self.assertEqual(body["num_bands"], 3)
        self.assertEqual(len(session.added), 1)
        self.assertEqual(session.commits, 1)
        self.assertEqual(session.refreshed, session.added)

    def test_creates_with_full_payload(self):
        session = _FakeSession(first=None)
        self._override_db(session)

        response = self.client.post(
            "/api/market-maker/configs",
            json=self._payload(
                market_title="My market",
                enabled=True,
                strategy="amm",
                num_bands=5,
                min_spread=0.01,
                max_spread=0.2,
                band_order_size=20.0,
                amm_liquidity=2000.0,
                max_collateral=900.0,
                sync_interval_seconds=60,
                min_order_size=2.0,
                min_price=0.05,
                max_price=0.95,
            ),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["market_title"], "My market")
        self.assertTrue(body["enabled"])
        self.assertEqual(body["strategy"], "amm")
        self.assertEqual(body["num_bands"], 5)
        self.assertEqual(body["min_spread"], 0.01)
        self.assertEqual(body["max_spread"], 0.2)
        self.assertEqual(body["band_order_size"], 20.0)
        self.assertEqual(body["amm_liquidity"], 2000.0)
        self.assertEqual(body["max_collateral"], 900.0)
        self.assertEqual(body["sync_interval_seconds"], 60)
        self.assertEqual(body["min_order_size"], 2.0)
        self.assertEqual(body["min_price"], 0.05)
        self.assertEqual(body["max_price"], 0.95)

    def test_updates_an_existing_config(self):
        existing = _config_row(id=7, market_title="Old title")
        session = _FakeSession(first=existing)
        self._override_db(session)

        response = self.client.post(
            "/api/market-maker/configs",
            json=self._payload(market_title="New title", enabled=True),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], 7)
        self.assertEqual(body["market_title"], "New title")
        self.assertTrue(body["enabled"])
        # The existing row was mutated in place, not re-created.
        self.assertEqual(session.added, [])
        self.assertEqual(session.commits, 1)
        self.assertEqual(existing.market_title, "New title")
        self.assertTrue(existing.enabled)

    def test_validation_rejects_short_condition_id(self):
        self._override_db(_FakeSession())
        response = self.client.post(
            "/api/market-maker/configs", json=self._payload(condition_id="x")
        )
        self.assertEqual(response.status_code, 422)

    def test_validation_rejects_unknown_strategy(self):
        self._override_db(_FakeSession())
        response = self.client.post(
            "/api/market-maker/configs",
            json=self._payload(strategy="martingale"),
        )
        self.assertEqual(response.status_code, 422)

    def test_validation_rejects_out_of_range_bands(self):
        self._override_db(_FakeSession())
        response = self.client.post("/api/market-maker/configs", json=self._payload(num_bands=0))
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/market-maker/configs", json=self._payload(num_bands=21))
        self.assertEqual(response.status_code, 422)

    def test_validation_rejects_bad_spreads(self):
        self._override_db(_FakeSession())
        response = self.client.post("/api/market-maker/configs", json=self._payload(min_spread=0.0))
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/market-maker/configs", json=self._payload(max_spread=0.6))
        self.assertEqual(response.status_code, 422)

    def test_validation_rejects_bad_order_size(self):
        self._override_db(_FakeSession())
        response = self.client.post(
            "/api/market-maker/configs", json=self._payload(band_order_size=0.5)
        )
        self.assertEqual(response.status_code, 422)

    def test_validation_rejects_bad_sync_interval(self):
        self._override_db(_FakeSession())
        response = self.client.post(
            "/api/market-maker/configs", json=self._payload(sync_interval_seconds=5)
        )
        self.assertEqual(response.status_code, 422)

    def test_validation_rejects_inverted_price_bounds(self):
        self._override_db(_FakeSession())
        response = self.client.post("/api/market-maker/configs", json=self._payload(min_price=0.0))
        self.assertEqual(response.status_code, 422)


class DeleteConfigTests(MarketMakerRouteTestCase):
    def test_missing_config_returns_404(self):
        self._override_db(_FakeSession(first=None))
        response = self.client.delete("/api/market-maker/configs/404")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Config not found"})
        self.mocks["stop_market_maker"].assert_not_awaited()

    def test_delete_disables_and_stops_the_maker(self):
        row = _config_row(id=9, enabled=True, status="running")
        self._override_db(_FakeSession(first=row))

        response = self.client.delete("/api/market-maker/configs/9")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "disabled", "id": 9})
        self.mocks["stop_market_maker"].assert_awaited_once_with(9)
        self.assertFalse(row.enabled)
        self.assertEqual(row.status, "idle")
        self.assertEqual(row.id, 9)


class StartConfigTests(MarketMakerRouteTestCase):
    def test_missing_config_returns_404(self):
        self._override_db(_FakeSession(first=None))
        response = self.client.post("/api/market-maker/configs/404/start")
        self.assertEqual(response.status_code, 404)
        self.mocks["start_market_maker"].assert_not_awaited()

    def test_start_marks_enabled_and_starts(self):
        row = _config_row(id=5, enabled=False)
        self._override_db(_FakeSession(first=row))

        response = self.client.post("/api/market-maker/configs/5/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "started", "config_id": 5})
        self.assertTrue(row.enabled)
        self.mocks["start_market_maker"].assert_awaited_once_with(5)

    def test_start_reports_already_running(self):
        row = _config_row(id=5)
        self._override_db(_FakeSession(first=row))
        self.mocks["start_market_maker"].return_value = False

        response = self.client.post("/api/market-maker/configs/5/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "already_running", "config_id": 5})

    def test_start_failure_propagates(self):
        row = _config_row(id=5)
        self._override_db(_FakeSession(first=row))
        self.mocks["start_market_maker"].side_effect = RuntimeError("loop exploded")

        with self.assertRaises(RuntimeError):
            self.client.post("/api/market-maker/configs/5/start")


class StopConfigTests(MarketMakerRouteTestCase):
    def test_missing_config_returns_404(self):
        self._override_db(_FakeSession(first=None))
        response = self.client.post("/api/market-maker/configs/404/stop")
        self.assertEqual(response.status_code, 404)
        self.mocks["stop_market_maker"].assert_not_awaited()

    def test_stop_marks_disabled_and_stops(self):
        row = _config_row(id=6, enabled=True)
        self._override_db(_FakeSession(first=row))

        response = self.client.post("/api/market-maker/configs/6/stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "stopped", "config_id": 6})
        self.assertFalse(row.enabled)
        self.mocks["stop_market_maker"].assert_awaited_once_with(6)

    def test_stop_reports_not_running(self):
        row = _config_row(id=6)
        self._override_db(_FakeSession(first=row))
        self.mocks["stop_market_maker"].return_value = False

        response = self.client.post("/api/market-maker/configs/6/stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "not_running", "config_id": 6})


class SyncConfigTests(MarketMakerRouteTestCase):
    def test_missing_config_returns_404(self):
        self._override_db(_FakeSession(first=None))
        response = self.client.post("/api/market-maker/configs/404/sync")
        self.assertEqual(response.status_code, 404)
        self.mocks["trigger_single_sync"].assert_not_awaited()

    def test_successful_sync_returns_the_result(self):
        row = _config_row(id=3)
        self._override_db(_FakeSession(first=row))
        self.mocks["trigger_single_sync"].return_value = {
            "midpoint": 0.5,
            "placed": 2,
        }

        response = self.client.post("/api/market-maker/configs/3/sync")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"success": True, "result": {"midpoint": 0.5, "placed": 2}, "detail": None},
        )
        self.mocks["trigger_single_sync"].assert_awaited_once_with(3)

    def test_sync_error_is_reported(self):
        row = _config_row(id=3)
        self._override_db(_FakeSession(first=row))
        self.mocks["trigger_single_sync"].return_value = {
            "error": "no midpoint",
        }

        response = self.client.post("/api/market-maker/configs/3/sync")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"success": False, "result": None, "detail": "no midpoint"},
        )


class MetricsTests(MarketMakerRouteTestCase):
    def test_metrics_reports_running_configs(self):
        self.mocks["get_running_maker_ids"].return_value = [1, 2, 3]

        response = self.client.get("/api/market-maker/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"running_configs": [1, 2, 3], "total_running": 3},
        )

    def test_metrics_with_no_running_makers(self):
        response = self.client.get("/api/market-maker/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"running_configs": [], "total_running": 0})


class ToResponseTests(unittest.TestCase):
    def test_to_response_maps_every_field(self):
        row = _config_row(
            last_sync_at=datetime(2026, 3, 3, tzinfo=UTC),
            updated_at=datetime(2026, 3, 4, tzinfo=UTC),
        )
        with patch(f"{CONFIG_PATH}.get_running_maker_ids", return_value=[1]):
            response = _to_response(row)

        self.assertEqual(response.id, 1)
        self.assertEqual(response.condition_id, "cond-1")
        self.assertEqual(response.token_id_yes, "token-yes")
        self.assertEqual(response.token_id_no, "token-no")
        self.assertEqual(response.market_title, "Bitcoin 5m Up or Down")
        self.assertTrue(response.enabled)
        self.assertEqual(response.strategy, "bands")
        self.assertEqual(response.num_bands, 3)
        self.assertEqual(response.min_spread, 0.02)
        self.assertEqual(response.max_spread, 0.10)
        self.assertEqual(response.band_order_size, 10.0)
        self.assertEqual(response.amm_liquidity, 1000.0)
        self.assertEqual(response.max_collateral, 500.0)
        self.assertEqual(response.sync_interval_seconds, 30)
        self.assertEqual(response.min_order_size, 1.0)
        self.assertEqual(response.min_price, 0.01)
        self.assertEqual(response.max_price, 0.99)
        self.assertEqual(response.status, "idle")
        self.assertEqual(response.last_sync_at, "2026-03-03T00:00:00+00:00")
        self.assertIsNone(response.last_error)
        self.assertEqual(response.total_orders_placed, 0)
        self.assertEqual(response.total_orders_cancelled, 0)
        self.assertEqual(response.total_volume_usdc, 0.0)
        self.assertEqual(response.current_open_orders, 0)
        self.assertTrue(response.is_running)
        self.assertEqual(response.created_at, "2026-01-01T00:00:00+00:00")
        self.assertEqual(response.updated_at, "2026-03-04T00:00:00+00:00")

    def test_request_schema_defaults(self):
        request = MarketMakerConfigRequest(
            condition_id="cond-1",
            token_id_yes="token-yes",
            token_id_no="token-no",
        )
        self.assertEqual(request.market_title, "")
        self.assertFalse(request.enabled)
        self.assertEqual(request.strategy, "bands")
        self.assertEqual(request.num_bands, 3)
        self.assertEqual(request.min_spread, 0.02)
        self.assertEqual(request.max_spread, 0.10)
        self.assertEqual(request.band_order_size, 10.0)
        self.assertEqual(request.amm_liquidity, 1000.0)
        self.assertEqual(request.max_collateral, 500.0)
        self.assertEqual(request.sync_interval_seconds, 30)
        self.assertEqual(request.min_order_size, 1.0)
        self.assertEqual(request.min_price, 0.01)
        self.assertEqual(request.max_price, 0.99)


if __name__ == "__main__":
    unittest.main()
