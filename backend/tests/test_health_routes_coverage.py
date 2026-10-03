"""Coverage tests for ``app.api.routes.health``.

Every external boundary is replaced with a double:

* the database engine is a ``MagicMock`` (or one whose ``connect``
  raises) so the liveness probe never touches a real database,
* the cache factory is patched at its source module,
* the gRPC analysis client factory is patched so no channel is
  ever opened,
* scheduler heartbeat helpers are patched so no cache is read,
* the DB session dependency is a ``FakeDB`` returning configured
  rows for the admin check.

No network, database or gRPC service is touched.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import app.api.routes.health as health_module
from app.api.routes.auth import get_current_user_from_token
from app.main import app
from app.models.user import User
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20
ADMIN_WALLET = "0x" + "cd" * 20


# ────────────── Test doubles ──────────────


class FakeQuery:
    """Chainable stand-in for a SQLAlchemy query."""

    def __init__(self, db):
        self.db = db

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.db.first_result


class FakeDB:
    """In-memory stand-in for a SQLAlchemy session."""

    def __init__(self, first_result=None):
        self.first_result = first_result

    def query(self, *entities):
        return FakeQuery(self)


def make_user(user_id=1, wallet=WALLET, is_admin=False):
    user = User(wallet_address=wallet, is_admin=is_admin)
    user.id = user_id
    return user


# ────────────── Helper functions ──────────────


class CacheBackendTests(unittest.TestCase):
    def test_reports_redis_backend(self):
        redis_cache = type("RedisCache", (), {})()
        with patch("app.utils.cache.get_cache", return_value=redis_cache):
            self.assertEqual(health_module._cache_backend(), "redis")

    def test_reports_memory_backend(self):
        with patch("app.utils.cache.get_cache", return_value=object()):
            self.assertEqual(health_module._cache_backend(), "memory")

    def test_reports_error_when_cache_unavailable(self):
        with patch("app.utils.cache.get_cache", side_effect=RuntimeError("boom")):
            self.assertEqual(health_module._cache_backend(), "error")


class GrpcBackendHealthTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_all_backends_healthy(self):
        client = MagicMock()
        client.health_check = AsyncMock(return_value=True)
        with patch(
            "app.grpc_clients.analysis_client.get_analysis_client",
            return_value=client,
        ):
            results = self._run(health_module._grpc_backend_health())
        self.assertEqual(results, {"llm_chain": True, "cli_agent": True})

    def test_unhealthy_backend_reports_false(self):
        client = MagicMock()
        client.health_check = AsyncMock(return_value=False)
        with patch(
            "app.grpc_clients.analysis_client.get_analysis_client",
            return_value=client,
        ):
            results = self._run(health_module._grpc_backend_health())
        self.assertEqual(results, {"llm_chain": False, "cli_agent": False})

    def test_client_factory_failure_reports_false(self):
        with patch(
            "app.grpc_clients.analysis_client.get_analysis_client",
            side_effect=RuntimeError("channel unavailable"),
        ):
            results = self._run(health_module._grpc_backend_health())
        self.assertEqual(results, {"llm_chain": False, "cli_agent": False})

    def test_health_check_failure_reports_false(self):
        client = MagicMock()
        client.health_check = AsyncMock(side_effect=RuntimeError("rpc failed"))
        with patch(
            "app.grpc_clients.analysis_client.get_analysis_client",
            return_value=client,
        ):
            results = self._run(health_module._grpc_backend_health())
        self.assertEqual(results, {"llm_chain": False, "cli_agent": False})


# ────────────── GET /health ──────────────


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._patches: list = []
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for patcher in self._patches:
            patcher.stop()

    def _patch(self, target, new=None, **kwargs):
        patcher = patch(target, new) if new is not None else patch(target, **kwargs)
        patcher.start()
        self._patches.append(patcher)
        return patcher

    def _patch_engine_ok(self):
        engine = MagicMock()
        self._patch("app.api.routes.health.engine", engine)
        return engine

    def _patch_schedulers(self, age=None, threshold=300):
        self._patch(
            "app.api.routes.health.scheduler_heartbeat_age",
            return_value=age,
        )
        self._patch(
            "app.api.routes.health.scheduler_stale_threshold",
            return_value=threshold,
        )

    def test_health_all_ok(self):
        self._patch_engine_ok()
        self._patch("app.api.routes.health._cache_backend", return_value="memory")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={"llm_chain": True, "cli_agent": True}),
        )
        self._patch_schedulers(age=None)

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("uptime_seconds", data)
        checks = data["checks"]
        self.assertTrue(checks["database"]["ok"])
        self.assertEqual(checks["cache"], {"ok": True, "backend": "memory"})
        self.assertEqual(checks["grpc_backends"], {"llm_chain": True, "cli_agent": True})
        for _name, entry in checks["schedulers"].items():
            self.assertIsNone(entry["heartbeat_age_seconds"])
            self.assertEqual(entry["stale_threshold_seconds"], 300)
            self.assertFalse(entry["stale"])

    def test_health_degraded_when_grpc_backend_down(self):
        self._patch_engine_ok()
        self._patch("app.api.routes.health._cache_backend", return_value="memory")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={"llm_chain": True, "cli_agent": False}),
        )
        self._patch_schedulers(age=None)

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "degraded")

    def test_health_degraded_when_scheduler_stale(self):
        self._patch_engine_ok()
        self._patch("app.api.routes.health._cache_backend", return_value="memory")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={"llm_chain": True, "cli_agent": True}),
        )
        self._patch_schedulers(age=9999.0, threshold=300)

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "degraded")
        for entry in data["checks"]["schedulers"].values():
            self.assertEqual(entry["heartbeat_age_seconds"], 9999.0)
            self.assertTrue(entry["stale"])

    def test_health_degraded_with_fresh_scheduler_heartbeat(self):
        self._patch_engine_ok()
        self._patch("app.api.routes.health._cache_backend", return_value="redis")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={"llm_chain": True, "cli_agent": True}),
        )
        self._patch_schedulers(age=12.34, threshold=300)

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        for entry in data["checks"]["schedulers"].values():
            self.assertEqual(entry["heartbeat_age_seconds"], 12.3)
            self.assertFalse(entry["stale"])

    def test_health_error_when_database_down(self):
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError("connection refused")
        self._patch("app.api.routes.health.engine", engine)
        self._patch("app.api.routes.health._cache_backend", return_value="memory")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={"llm_chain": True, "cli_agent": True}),
        )
        self._patch_schedulers(age=None)

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 503)
        data = response.json()
        self.assertEqual(data["status"], "error")
        check = data["checks"]["database"]
        self.assertFalse(check["ok"])
        self.assertIn("connection refused", check["error"])

    def test_health_ok_when_grpc_results_empty(self):
        self._patch_engine_ok()
        self._patch("app.api.routes.health._cache_backend", return_value="memory")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={}),
        )
        self._patch_schedulers(age=None)

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["checks"]["grpc_backends"], {})


# ────────────── GET /metrics (admin only) ──────────────


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def _authenticate(self, user_id=1, is_admin=False):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": user_id,
            "wallet_address": WALLET,
            "is_admin": is_admin,
        }

    def _set_db_user(self, user):
        app.dependency_overrides[get_db] = lambda: FakeDB(first_result=user)

    def test_metrics_requires_authentication(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 401)

    def test_metrics_forbidden_for_non_admin(self):
        self._authenticate(is_admin=False)
        self._set_db_user(make_user(is_admin=False))
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    def test_metrics_forbidden_when_user_not_found(self):
        self._authenticate(user_id=999)
        self._set_db_user(None)
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 403)

    def test_metrics_forbidden_when_user_id_missing(self):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": None,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        self._set_db_user(None)
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 403)

    def test_metrics_admin_renders_prometheus_text(self):
        self._authenticate(is_admin=True)
        self._set_db_user(make_user(is_admin=True))

        def heartbeat_age(name):
            return 12.5 if name == "stop_loss_monitor" else None

        with (
            patch(
                "app.api.routes.health.render_prometheus",
                return_value="# HELP app_uptime_seconds up\n",
            ),
            patch(
                "app.api.routes.health.scheduler_heartbeat_age",
                side_effect=heartbeat_age,
            ),
        ):
            response = self.client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("# HELP app_uptime_seconds up", response.text)
        self.assertIn(
            'scheduler_heartbeat_age_seconds{scheduler="stop_loss_monitor"} 12.5',
            response.text,
        )
        # Schedulers without a heartbeat contribute no line.
        self.assertNotIn('scheduler="market_maker"', response.text)


if __name__ == "__main__":
    unittest.main()
