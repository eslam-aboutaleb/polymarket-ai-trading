"""Coverage tests for the debug API routes (app/api/routes/debug.py).

The ``require_admin`` dependency is exercised through ``TestClient(app)``
with the auth and DB dependencies overridden.  Every health-check helper
is tested directly with its external I/O (SQLAlchemy session, redis,
TCP connections, background task list, inverse-bot metrics, MCP path
probing) mocked out.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.debug import (
    ServiceHealth,
    _check_background_tasks,
    _check_grpc_service,
    _check_inverse_bot_monitor,
    _check_postgres,
    _check_redis,
    _check_research_mcp,
    _format_uptime,
)
from app.main import app
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20


def _log_entry(**overrides):
    entry = {
        "request_id": "req-1",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "method": "GET",
        "path": "/api/trades",
        "query": "",
        "status_code": 200,
        "duration_ms": 12.3,
        "level": "info",
        "client_ip": "hashed",
        "user_agent": "test-agent",
        "error_detail": None,
    }
    entry.update(overrides)
    return entry


def _endpoint_stat(**overrides):
    stat = {
        "method": "GET",
        "path": "/api/trades",
        "call_count": 10,
        "error_count": 1,
        "warning_count": 2,
        "total_duration_ms": 100.0,
        "min_duration_ms": 1.0,
        "max_duration_ms": 20.0,
        "last_called": "2026-01-01T00:00:00+00:00",
        "last_status": 200,
    }
    stat.update(overrides)
    return stat


def _fake_db(admin=True, user_id=1):
    db = MagicMock()
    user = MagicMock()
    user.id = user_id
    user.wallet_address = WALLET
    user.is_admin = admin
    db.query.return_value.filter.return_value.first.return_value = user
    return db


class RequireAdminTests(unittest.TestCase):
    """Tests for the require_admin dependency via the /logs endpoint."""

    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": True}
        self.addCleanup(app.dependency_overrides.clear)

    def test_requires_authentication(self):
        response = self.client.get("/api/debug/logs")
        self.assertEqual(response.status_code, 401)

    def test_rejects_non_admin_user(self):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 2,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        app.dependency_overrides[get_db] = lambda: _fake_db(admin=False)
        response = self.client.get("/api/debug/logs")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    def test_rejects_missing_user_id(self):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": None,
            "wallet_address": None,
            "is_admin": False,
        }
        app.dependency_overrides[get_db] = lambda: _fake_db()
        response = self.client.get("/api/debug/logs")
        self.assertEqual(response.status_code, 403)

    def test_rejects_when_user_not_found(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 99,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        app.dependency_overrides[get_db] = lambda: db
        response = self.client.get("/api/debug/logs")
        self.assertEqual(response.status_code, 403)

    def test_returns_404_when_debug_endpoints_disabled(self):
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: _fake_db()
        with patch(
            "app.api.routes.debug.settings",
            MagicMock(debug_endpoints_active=False),
        ):
            response = self.client.get("/api/debug/logs")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Debug endpoints disabled")

    def test_admin_dependency_returns_user_info(self):
        from app.api.routes.debug import require_admin

        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: _fake_db()
        result = require_admin(
            current_user=self.user,
            db=_fake_db(),
        )
        self.assertEqual(result["user_id"], 1)
        self.assertEqual(result["wallet_address"], WALLET)
        self.assertTrue(result["is_admin"])


class GetLogsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": True}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: _fake_db()
        self.addCleanup(app.dependency_overrides.clear)

    def _entries(self):
        return [
            _log_entry(request_id="a", level="info", path="/api/trades", method="GET"),
            _log_entry(
                request_id="b",
                level="warning",
                path="/api/portfolio",
                method="POST",
                status_code=400,
            ),
            _log_entry(
                request_id="c",
                level="error",
                path="/api/debug/logs",
                method="DELETE",
                status_code=500,
            ),
        ]

    def test_returns_entries(self):
        entries = self._entries()
        with patch("app.api.routes.debug.get_log_entries", lambda: entries):
            response = self.client.get("/api/debug/logs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["filtered"], 3)
        self.assertEqual([e["request_id"] for e in data["entries"]], ["a", "b", "c"])

    def test_level_filter(self):
        entries = self._entries()
        with patch("app.api.routes.debug.get_log_entries", lambda: entries):
            response = self.client.get("/api/debug/logs", params={"level": "ERROR"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["filtered"], 1)
        self.assertEqual(data["entries"][0]["request_id"], "c")

    def test_path_filter(self):
        entries = self._entries()
        with patch("app.api.routes.debug.get_log_entries", lambda: entries):
            response = self.client.get("/api/debug/logs", params={"path": "portfolio"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["filtered"], 1)
        self.assertEqual(data["entries"][0]["request_id"], "b")

    def test_method_filter(self):
        entries = self._entries()
        with patch("app.api.routes.debug.get_log_entries", lambda: entries):
            response = self.client.get("/api/debug/logs", params={"method": "post"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["filtered"], 1)
        self.assertEqual(data["entries"][0]["request_id"], "b")

    def test_combined_filters(self):
        entries = self._entries() + [
            _log_entry(
                request_id="d", level="error", path="/api/trades", method="GET", status_code=500
            ),
        ]
        with patch("app.api.routes.debug.get_log_entries", lambda: entries):
            response = self.client.get(
                "/api/debug/logs",
                params={"level": "error", "path": "trades", "method": "get"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["filtered"], 1)
        self.assertEqual(data["entries"][0]["request_id"], "d")

    def test_limit_slices_results(self):
        entries = [_log_entry(request_id=str(i)) for i in range(10)]
        with patch("app.api.routes.debug.get_log_entries", lambda: entries):
            response = self.client.get("/api/debug/logs", params={"limit": 3})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["filtered"], 3)
        self.assertEqual(len(data["entries"]), 3)

    def test_limit_below_minimum_rejected(self):
        response = self.client.get("/api/debug/logs", params={"limit": 0})
        self.assertEqual(response.status_code, 422)

    def test_limit_above_maximum_rejected(self):
        response = self.client.get("/api/debug/logs", params={"limit": 1001})
        self.assertEqual(response.status_code, 422)

    def test_empty_entries(self):
        with patch("app.api.routes.debug.get_log_entries", list):
            response = self.client.get("/api/debug/logs")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["filtered"], 0)
        self.assertEqual(data["entries"], [])


class GetStatsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": True}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: _fake_db()
        self.addCleanup(app.dependency_overrides.clear)

    def test_returns_aggregated_stats(self):
        raw = {
            "GET /api/trades": _endpoint_stat(
                call_count=10, error_count=1, warning_count=2, total_duration_ms=100.0
            ),
            "POST /api/portfolio": _endpoint_stat(
                method="POST",
                path="/api/portfolio",
                call_count=5,
                error_count=0,
                warning_count=1,
                total_duration_ms=50.0,
            ),
        }
        with patch("app.api.routes.debug.get_endpoint_stats", lambda: raw):
            response = self.client.get("/api/debug/stats")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_requests"], 15)
        self.assertEqual(data["total_errors"], 1)
        self.assertEqual(data["total_warnings"], 3)
        self.assertEqual(data["error_rate"], round(1 / 15 * 100, 2))
        # Sorted by call_count descending
        self.assertEqual(data["endpoints"][0]["path"], "/api/trades")
        self.assertEqual(data["endpoints"][0]["avg_duration_ms"], 10.0)
        self.assertEqual(data["endpoints"][1]["path"], "/api/portfolio")
        self.assertEqual(data["endpoints"][1]["avg_duration_ms"], 10.0)

    def test_zero_call_count_yields_zero_avg(self):
        raw = {
            "GET /api/trades": _endpoint_stat(call_count=0, total_duration_ms=0.0),
        }
        with patch("app.api.routes.debug.get_endpoint_stats", lambda: raw):
            response = self.client.get("/api/debug/stats")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_requests"], 0)
        self.assertEqual(data["error_rate"], 0)
        self.assertEqual(data["endpoints"][0]["avg_duration_ms"], 0)

    def test_empty_stats(self):
        with patch("app.api.routes.debug.get_endpoint_stats", dict):
            response = self.client.get("/api/debug/stats")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_requests"], 0)
        self.assertEqual(data["total_errors"], 0)
        self.assertEqual(data["total_warnings"], 0)
        self.assertEqual(data["error_rate"], 0)
        self.assertEqual(data["endpoints"], [])


class DeleteLogsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": True}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: _fake_db()
        self.addCleanup(app.dependency_overrides.clear)

    def test_deletes_logs(self):
        with patch("app.api.routes.debug.clear_logs", lambda: 42):
            response = self.client.delete("/api/debug/logs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"cleared": 42, "message": "Cleared 42 log entries"},
        )

    def test_deletes_zero_logs(self):
        with patch("app.api.routes.debug.clear_logs", lambda: 0):
            response = self.client.delete("/api/debug/logs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cleared"], 0)


class FormatUptimeTests(unittest.TestCase):
    def test_days_hours_minutes_seconds(self):
        self.assertEqual(_format_uptime(90061), "1d 1h 1m 1s")

    def test_hours_minutes_seconds(self):
        self.assertEqual(_format_uptime(3661), "1h 1m 1s")

    def test_minutes_seconds(self):
        self.assertEqual(_format_uptime(61), "1m 1s")

    def test_seconds_only(self):
        self.assertEqual(_format_uptime(5), "5s")

    def test_zero(self):
        self.assertEqual(_format_uptime(0), "0s")


class CheckPostgresTests(unittest.TestCase):
    def test_healthy(self):
        db = MagicMock()
        db.execute = MagicMock()
        db.close = MagicMock()
        with patch("app.utils.database.SessionLocal", lambda: db):
            result = asyncio_result(_check_postgres())
        self.assertEqual(result.name, "PostgreSQL")
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.detail, "Connected")
        self.assertIsNotNone(result.latency_ms)
        db.execute.assert_called_once()
        db.close.assert_called_once()

    def test_unhealthy_when_session_raises(self):
        with patch(
            "app.utils.database.SessionLocal",
            MagicMock(side_effect=RuntimeError("db down")),
        ):
            result = asyncio_result(_check_postgres())
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("db down", result.detail)

    def test_unhealthy_when_execute_raises(self):
        db = MagicMock()
        db.execute = MagicMock(side_effect=RuntimeError("query failed"))
        db.close = MagicMock()
        with patch("app.utils.database.SessionLocal", lambda: db):
            result = asyncio_result(_check_postgres())
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("query failed", result.detail)
        db.close.assert_called_once()


class CheckRedisTests(unittest.TestCase):
    def test_unknown_when_url_not_set(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("REDIS_URL", None)
            result = asyncio_result(_check_redis())
        self.assertEqual(result.name, "Redis")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.detail, "REDIS_URL not set")

    def test_healthy(self):
        r = MagicMock()
        r.ping = MagicMock()
        r.close = MagicMock()
        with (
            patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}),
            patch("redis.from_url", lambda url: r),
        ):
            result = asyncio_result(_check_redis())
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.detail, "Connected")
        r.ping.assert_called_once()
        r.close.assert_called_once()

    def test_unhealthy_on_error(self):
        with (
            patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}),
            patch(
                "redis.from_url",
                MagicMock(side_effect=RuntimeError("redis down")),
            ),
        ):
            result = asyncio_result(_check_redis())
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("redis down", result.detail)


class CheckGrpcServiceTests(unittest.TestCase):
    def test_healthy(self):
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(MagicMock(), writer)),
        ):
            result = asyncio_result(_check_grpc_service("Test gRPC", "localhost", 50051))
        self.assertEqual(result.name, "Test gRPC")
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.detail, "localhost:50051")
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    def test_unhealthy_on_timeout(self):
        with patch(
            "asyncio.open_connection",
            AsyncMock(side_effect=TimeoutError()),
        ):
            result = asyncio_result(_check_grpc_service("Test gRPC", "localhost", 50051))
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("Timeout connecting to localhost:50051", result.detail)

    def test_unhealthy_on_error(self):
        with patch(
            "asyncio.open_connection",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = asyncio_result(_check_grpc_service("Test gRPC", "localhost", 50051))
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("boom", result.detail)


class CheckBackgroundTasksTests(unittest.TestCase):
    def test_healthy_when_all_alive(self):
        tasks = [MagicMock(done=MagicMock(return_value=False)) for _ in range(2)]
        with patch("app.main._background_tasks", tasks):
            result = asyncio_result(_check_background_tasks())
        self.assertEqual(result.status, "healthy")
        self.assertEqual(result.detail, "2/2 tasks running")

    def test_unhealthy_when_all_done(self):
        tasks = [MagicMock(done=MagicMock(return_value=True)) for _ in range(2)]
        with patch("app.main._background_tasks", tasks):
            result = asyncio_result(_check_background_tasks())
        self.assertEqual(result.status, "unhealthy")
        self.assertEqual(result.detail, "0/2 tasks running")

    def test_warning_when_partially_alive(self):
        tasks = [
            MagicMock(done=MagicMock(return_value=True)),
            MagicMock(done=MagicMock(return_value=False)),
        ]
        with patch("app.main._background_tasks", tasks):
            result = asyncio_result(_check_background_tasks())
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.detail, "1/2 tasks running")

    def test_unknown_when_no_tasks(self):
        with patch("app.main._background_tasks", []):
            result = asyncio_result(_check_background_tasks())
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.detail, "0/0 tasks running")

    def test_unknown_on_exception(self):
        class _Exploding:
            def __iter__(self):
                raise RuntimeError("boom")

            def __len__(self):
                raise RuntimeError("boom")

        with patch("app.main._background_tasks", _Exploding()):
            result = asyncio_result(_check_background_tasks())
        self.assertEqual(result.status, "unknown")
        self.assertIn("boom", result.detail)


class CheckInverseBotMonitorTests(unittest.TestCase):
    def test_healthy_when_running(self):
        metrics = {
            "running": True,
            "evaluations_total": 10,
            "reversals_total": 2,
            "inflight": 1,
        }
        with patch(
            "app.services.inverse_bot_monitor.get_inverse_bot_metrics",
            lambda: metrics,
        ):
            result = asyncio_result(_check_inverse_bot_monitor())
        self.assertEqual(result.name, "Inverse Bot Monitor")
        self.assertEqual(result.status, "healthy")
        self.assertIn("running=True", result.detail)
        self.assertIn("evaluations=10", result.detail)
        self.assertIn("reversals=2", result.detail)
        self.assertIn("inflight=1", result.detail)

    def test_unhealthy_when_not_running(self):
        metrics = {"running": False, "evaluations_total": 0, "reversals_total": 0, "inflight": 0}
        with patch(
            "app.services.inverse_bot_monitor.get_inverse_bot_metrics",
            lambda: metrics,
        ):
            result = asyncio_result(_check_inverse_bot_monitor())
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("running=False", result.detail)

    def test_unknown_on_exception(self):
        with patch(
            "app.services.inverse_bot_monitor.get_inverse_bot_metrics",
            MagicMock(side_effect=RuntimeError("boom")),
        ):
            result = asyncio_result(_check_inverse_bot_monitor())
        self.assertEqual(result.status, "unknown")
        self.assertIn("boom", result.detail)


class CheckResearchMcpTests(unittest.TestCase):
    def test_healthy_when_server_path_exists(self):
        import tempfile

        with (
            tempfile.NamedTemporaryFile(suffix=".py") as tmp,
            patch.dict("os.environ", {"RESEARCH_MCP_SERVER_PATH": tmp.name}),
        ):
            result = asyncio_result(_check_research_mcp())
        self.assertEqual(result.name, "Research MCP")
        self.assertEqual(result.status, "healthy")
        self.assertIn(f"server={tmp.name}", result.detail)

    def test_healthy_managed_by_containers(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("RESEARCH_MCP_SERVER_PATH", None)
            with patch(
                "pathlib.Path.exists",
                MagicMock(return_value=False),
            ):
                result = asyncio_result(_check_research_mcp())
        self.assertEqual(result.status, "healthy")
        self.assertIn("Managed by AI service containers", result.detail)

    def test_unhealthy_on_exception(self):
        with patch(
            "pathlib.Path.exists",
            MagicMock(side_effect=RuntimeError("boom")),
        ):
            result = asyncio_result(_check_research_mcp())
        self.assertEqual(result.status, "unhealthy")
        self.assertIn("boom", result.detail)


class ComprehensiveHealthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": True}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: _fake_db()
        self.addCleanup(app.dependency_overrides.clear)

    def test_health_endpoint(self):
        grpc_mock = AsyncMock(return_value=ServiceHealth(name="gRPC", status="healthy"))
        with (
            patch("app.api.routes.debug.get_uptime_seconds", lambda: 123.456),
            patch.multiple(
                "app.api.routes.debug",
                _check_postgres=AsyncMock(
                    return_value=ServiceHealth(name="PostgreSQL", status="healthy"),
                ),
                _check_redis=AsyncMock(
                    return_value=ServiceHealth(name="Redis", status="healthy"),
                ),
                _check_grpc_service=grpc_mock,
                _check_background_tasks=AsyncMock(
                    return_value=ServiceHealth(
                        name="Background Tasks",
                        status="healthy",
                    ),
                ),
                _check_inverse_bot_monitor=AsyncMock(
                    return_value=ServiceHealth(
                        name="Inverse Bot Monitor",
                        status="healthy",
                    ),
                ),
                _check_research_mcp=AsyncMock(
                    return_value=ServiceHealth(name="Research MCP", status="healthy"),
                ),
            ),
        ):
            response = self.client.get("/api/debug/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["uptime_seconds"], 123.5)
        self.assertEqual(data["uptime_human"], "2m 3s")
        self.assertIn("server_time", data)
        self.assertIn("python_version", data)
        self.assertEqual(len(data["services"]), 7)
        names = [s["name"] for s in data["services"]]
        self.assertIn("PostgreSQL", names)
        self.assertIn("Redis", names)
        self.assertIn("Background Tasks", names)

    def test_health_endpoint_reads_env_config(self):
        grpc_mock = AsyncMock(return_value=ServiceHealth(name="gRPC", status="healthy"))
        env = {
            "LLM_CHAIN_HOST": "llm-custom",
            "LLM_CHAIN_PORT": "9999",
            "CLI_AGENT_HOST": "cli-custom",
            "CLI_AGENT_PORT": "8888",
        }
        with (
            patch.dict("os.environ", env),
            patch("app.api.routes.debug.get_uptime_seconds", lambda: 1.0),
            patch.multiple(
                "app.api.routes.debug",
                _check_postgres=AsyncMock(
                    return_value=ServiceHealth(name="PostgreSQL", status="healthy"),
                ),
                _check_redis=AsyncMock(
                    return_value=ServiceHealth(name="Redis", status="healthy"),
                ),
                _check_grpc_service=grpc_mock,
                _check_background_tasks=AsyncMock(
                    return_value=ServiceHealth(
                        name="Background Tasks",
                        status="healthy",
                    ),
                ),
                _check_inverse_bot_monitor=AsyncMock(
                    return_value=ServiceHealth(
                        name="Inverse Bot Monitor",
                        status="healthy",
                    ),
                ),
                _check_research_mcp=AsyncMock(
                    return_value=ServiceHealth(name="Research MCP", status="healthy"),
                ),
            ),
        ):
            response = self.client.get("/api/debug/health")
        self.assertEqual(response.status_code, 200)
        calls = grpc_mock.await_args_list
        self.assertEqual(
            [c.args for c in calls],
            [
                ("LLM Chain (gRPC)", "llm-custom", 9999),
                ("CLI Agent (gRPC)", "cli-custom", 8888),
            ],
        )


def asyncio_result(coro):
    """Run a coroutine to completion on a fresh event loop."""
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
