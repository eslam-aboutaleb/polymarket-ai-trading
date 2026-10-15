"""Plan B3 checklist: rate limiter resilience and health/debug hardening.

Where ``test_b1_error_masking.py`` asserts the *masking* properties, this
module asserts the properties sub-plan B3 is judged on, in one place:

* the limiter resolves its storage from ``REDIS_URL``, falls back to
  ``memory://`` (never raising) when it is unset, and logs a warning that
  names the per-worker consequence — but only outside the
  local/dev/test environments, where a single worker makes per-process
  counting harmless;
* ``/health`` keeps its ``ok: False`` flag, its ``degraded``/``error``
  status logic and its 503, while putting nothing but a generic sentence
  in the payload;
* the admin-gated debug checks keep returning a generic ``detail`` (the
  full exception still reaches the log);
* the paid news generation routes and the public LLM-backed analysis
  routes each carry their own per-endpoint budget below the global one.

The environment is read through the module's own injectable resolvers, so
no test has to reload ``app.security.rate_limit`` — a reload would rebuild
the ``Limiter`` and drop every counter the rest of the running process
holds.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from limits import parse

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.debug import (
    _check_grpc_service,
    _check_postgres,
    _check_redis,
    _check_research_mcp,
)
from app.api.routes.health import _cache_backend, _grpc_backend_health
from app.main import app
from app.security import rate_limit as rate_limit_module
from app.security.rate_limit import _resolve_storage_uri, limiter
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20


def _route_limit(function):
    """Return the ``limits`` item a stub dependency was registered with."""
    name = f"{function.__module__}.{function.__name__}"
    registered = limiter._route_limits.get(name)
    assert registered, f"{name} is not registered with the limiter"
    return registered[0].limit


class LimiterStorageResolutionTests(unittest.TestCase):
    """Storage selection and the non-local warning (plan B3.1 / B3.5)."""

    def _resolve(self, **environment):
        return _resolve_storage_uri(environment)

    def test_redis_url_wins_when_set(self):
        self.assertEqual(self._resolve(REDIS_URL="redis://cache:6379/0"), "redis://cache:6379/0")

    def test_redis_url_wins_over_a_non_local_environment(self):
        with patch.object(rate_limit_module.logger, "warning") as warning:
            uri = self._resolve(REDIS_URL="redis://cache:6379/0", ENVIRONMENT="production")
        self.assertEqual(uri, "redis://cache:6379/0")
        warning.assert_not_called()

    def test_blank_redis_url_treated_as_unset(self):
        with self.assertLogs(rate_limit_module.logger, level="WARNING"):
            uri = self._resolve(REDIS_URL="   ", ENVIRONMENT="production")
        self.assertEqual(uri, "memory://")

    def test_unset_redis_url_warns_in_a_non_local_environment(self):
        with self.assertLogs(rate_limit_module.logger, level="WARNING") as captured:
            uri = self._resolve(ENVIRONMENT="production")

        self.assertEqual(uri, "memory://")
        message = "\n".join(captured.output)
        self.assertIn("REDIS_URL", message)
        self.assertIn("per-process", message)
        # The operator remedy is stated, not just the problem.
        self.assertIn("Set REDIS_URL", message)

    def test_unset_redis_url_warns_for_an_unknown_environment(self):
        """An unrecognised environment is treated as non-local (fail loud)."""
        with self.assertLogs(rate_limit_module.logger, level="WARNING"):
            self.assertEqual(self._resolve(ENVIRONMENT="staging"), "memory://")

    def test_unset_environment_variable_falls_back_to_the_setting(self):
        """``ENVIRONMENT`` unset resolves through settings, not a blank."""
        settings = MagicMock()
        settings.environment = "production"
        with patch("app.config.get_settings", return_value=settings):
            self.assertEqual(rate_limit_module._resolve_environment({}), "production")

    def test_unresolvable_settings_do_not_break_the_resolution(self):
        """A settings failure degrades to the memory fallback, never a raise."""
        with (
            patch("app.config.get_settings", side_effect=RuntimeError("settings unavailable")),
            self.assertLogs(rate_limit_module.logger, level="WARNING"),
        ):
            self.assertEqual(rate_limit_module._resolve_storage_uri({}), "memory://")

    def test_environment_variable_takes_precedence_over_the_setting(self):
        settings = MagicMock()
        settings.environment = "production"
        with (
            patch("app.config.get_settings", return_value=settings),
            patch.object(rate_limit_module.logger, "warning") as warning,
        ):
            resolved = rate_limit_module._resolve_environment({"ENVIRONMENT": "test"})
        self.assertEqual(resolved, "test")
        warning.assert_not_called()

    def test_unset_redis_url_stays_quiet_in_local_environments(self):
        for environment in ("dev", "development", "local", "test", "DEVELOPMENT"):
            with self.subTest(environment=environment):
                with patch.object(rate_limit_module.logger, "warning") as warning:
                    uri = self._resolve(ENVIRONMENT=environment)
                self.assertEqual(uri, "memory://")
                warning.assert_not_called()

    def test_module_degraded_to_memory_under_the_test_environment(self):
        """Import-time resolution picked the memory backend and stayed enabled."""
        self.assertEqual(rate_limit_module.storage_uri, "memory://")
        self.assertTrue(limiter.enabled)

    def test_environment_resolution_is_injectable(self):
        self.assertEqual(_resolve_storage_uri({}, environment="Production"), "memory://")
        self.assertEqual(
            _resolve_storage_uri({"REDIS_URL": "redis://x"}, environment="production"),
            "redis://x",
        )

    def test_local_environments_match_the_settings_definition(self):
        from app.config import Settings

        for environment in sorted(rate_limit_module._LOCAL_ENVIRONMENTS):
            with self.subTest(environment=environment):
                self.assertTrue(
                    Settings(jwt_secret_key="a" * 32, environment=environment).is_local_environment
                )


class HealthDetailMaskingTests(unittest.TestCase):
    """``/health`` stays a liveness signal, not an error oracle (B3.2)."""

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

    def _patch_database_down(self, message):
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError(message)
        self._patch("app.api.routes.health.engine", new=engine)

    def _patch_dependencies_ok(self):
        self._patch("app.api.routes.health._cache_backend", return_value="memory")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={"llm_chain": True, "cli_agent": True}),
        )
        self._patch("app.api.routes.health.scheduler_heartbeat_age", return_value=None)
        self._patch("app.api.routes.health.scheduler_stale_threshold", return_value=300)

    def _patch_database_ok(self):
        self._patch("app.api.routes.health.engine", new=MagicMock())

    def test_database_failure_reports_503_without_exception_text(self):
        self._patch_database_down("could not translate host name db.internal:5432")
        self._patch_dependencies_ok()

        with self.assertLogs("app.api.routes.health", level="ERROR") as captured:
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        check = payload["checks"]["database"]
        self.assertFalse(check["ok"])
        # The payload names the failed check and nothing else.
        self.assertEqual(check["error"], "Database check failed")
        self.assertNotIn("db.internal", response.text)
        self.assertNotIn("translate host name", response.text)
        self.assertNotIn("RuntimeError", response.text)
        # The operator can still recover the cause from the log.
        self.assertIn(
            "RuntimeError: could not translate host name db.internal:5432",
            "\n".join(captured.output),
        )

    def test_healthy_probe_keeps_the_payload_minimal(self):
        self._patch_database_ok()
        self._patch_dependencies_ok()

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["checks"]["database"], {"ok": True})

    def test_dependency_helpers_report_no_exception_detail(self):
        """Cache and gRPC probes degrade to booleans, never to messages."""
        self._patch("app.utils.cache.get_cache", side_effect=RuntimeError("cache-secret"))
        self._patch(
            "app.grpc_clients.analysis_client.get_analysis_client",
            side_effect=RuntimeError("grpc-secret 10.0.0.5:50051"),
        )

        self.assertEqual(_cache_backend(), "error")
        grpc_results = asyncio.run(_grpc_backend_health())
        self.assertEqual(grpc_results, {"llm_chain": False, "cli_agent": False})


class DebugDetailMaskingTests(unittest.TestCase):
    """Admin-gated debug checks report status, not exceptions (B3.3)."""

    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_failing_checks_return_a_generic_detail_and_log_the_rest(self):
        with (
            patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}),
            patch("redis.from_url", MagicMock(side_effect=RuntimeError("redis-secret"))),
            self.assertLogs("app.api.routes.debug", level="ERROR") as redis_logs,
        ):
            redis_check = self._run(_check_redis())
        self.assertEqual(redis_check.status, "unhealthy")
        self.assertEqual(redis_check.detail, "Check failed")
        self.assertIn("RuntimeError: redis-secret", "\n".join(redis_logs.output))

        with (
            patch("asyncio.open_connection", AsyncMock(side_effect=RuntimeError("grpc-secret"))),
            self.assertLogs("app.api.routes.debug", level="ERROR") as grpc_logs,
        ):
            grpc_check = self._run(_check_grpc_service("LLM Chain (gRPC)", "llm-chain", 50051))
        self.assertEqual(grpc_check.status, "unhealthy")
        self.assertEqual(grpc_check.detail, "Check failed")
        self.assertIn("RuntimeError: grpc-secret", "\n".join(grpc_logs.output))

        with (
            patch(
                "app.utils.database.SessionLocal",
                MagicMock(side_effect=RuntimeError("db-secret")),
            ),
            self.assertLogs("app.api.routes.debug", level="ERROR") as db_logs,
        ):
            postgres_check = self._run(_check_postgres())
        self.assertEqual(postgres_check.status, "unhealthy")
        self.assertEqual(postgres_check.detail, "Check failed")
        self.assertIn("RuntimeError: db-secret", "\n".join(db_logs.output))

        with (
            patch("pathlib.Path.exists", MagicMock(side_effect=RuntimeError("mcp-secret"))),
            self.assertLogs("app.api.routes.debug", level="ERROR") as mcp_logs,
        ):
            mcp_check = self._run(_check_research_mcp())
        self.assertEqual(mcp_check.status, "unhealthy")
        self.assertEqual(mcp_check.detail, "Check failed")
        self.assertIn("RuntimeError: mcp-secret", "\n".join(mcp_logs.output))

    def _override_authenticated_non_admin(self):
        """Point the auth dependency at a non-admin user with a real DB."""
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 2,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        non_admin_db = MagicMock()
        user = MagicMock()
        user.is_admin = False
        non_admin_db.query.return_value.filter.return_value.first.return_value = user
        app.dependency_overrides[get_db] = lambda: non_admin_db

    def test_debug_routes_are_admin_gated(self):
        """Every debug route rejects an anonymous and a non-admin caller."""
        routes = (
            ("GET", "/api/debug/logs"),
            ("GET", "/api/debug/stats"),
            ("GET", "/api/debug/health"),
            ("DELETE", "/api/debug/logs"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path).status_code, 401)

                self._override_authenticated_non_admin()
                try:
                    response = self.client.request(method, path)
                finally:
                    app.dependency_overrides.clear()
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"], "Admin access required")

    def test_debug_source_hides_no_exception_text(self):
        """Guard the module against a re-introduced ``detail=str(exc)`` leak."""
        import inspect
        import re

        from app.api.routes import debug

        source = inspect.getsource(debug)
        self.assertIsNone(
            re.search(r"detail\s*=\s*str\((?:e|exc)\)", source),
            "debug.py must not put exception text in a response detail",
        )


class PerEndpointLimitTests(unittest.TestCase):
    """Expensive routes carry their own budget below the global one (B3.4)."""

    def test_news_generation_stubs_are_limited_to_five_per_minute(self):
        from app.api.routes.news import (
            _news_generate_rate_limit,
            _news_generate_stream_rate_limit,
            _news_refresh_feed_rate_limit,
        )

        for function in (
            _news_generate_rate_limit,
            _news_generate_stream_rate_limit,
            _news_refresh_feed_rate_limit,
        ):
            with self.subTest(limit=function.__name__):
                self.assertEqual(_route_limit(function), parse("5/minute"))

    def test_public_analysis_routes_are_limited_below_the_global_budget(self):
        from app.api.routes.analysis import _analysis_rate_limit, _analysis_scan_rate_limit

        self.assertEqual(_route_limit(_analysis_rate_limit), parse("10/minute"))
        self.assertEqual(_route_limit(_analysis_scan_rate_limit), parse("20/minute"))

    def test_markets_trader_analysis_stream_is_limited(self):
        from app.api.routes.markets import _trader_analysis_stream_rate_limit

        self.assertEqual(_route_limit(_trader_analysis_stream_rate_limit), parse("5/minute"))

    def test_news_generation_enforces_its_endpoint_budget(self):
        """Six authenticated calls in a minute hit the endpoint's own 5/minute."""
        service = MagicMock()
        service.generate_for_market = AsyncMock(return_value={"headline": "Generated"})
        limiter.reset()
        self.addCleanup(limiter.reset)
        app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
        self.addCleanup(app.dependency_overrides.clear)

        with patch("app.api.routes.news.get_news_service", return_value=service):
            client = TestClient(app)
            body = {"condition_id": "c1", "question": "Q?"}
            for _ in range(5):
                self.assertEqual(client.post("/api/news/generate", json=body).status_code, 200)
            self.assertEqual(client.post("/api/news/generate", json=body).status_code, 429)


if __name__ == "__main__":
    unittest.main()
