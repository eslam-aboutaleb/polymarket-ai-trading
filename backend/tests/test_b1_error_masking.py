"""Plan B1 checklist: error masking and news auth, end to end.

The per-route suites assert each masked message; this module asserts the
properties the sub-plan is judged on, in one place:

* the paid news generation/refresh routes reject an anonymous caller with
  401 and still serve an authenticated one;
* a failing generation logs the full exception but renders a generic
  message in both the JSON body and the SSE error event;
* ``/health`` reports a database outage without any exception text;
* the admin-gated debug checks and the orchestration route return generic
  details while their full exception text reaches the log.

Every external boundary is a test double (a mocked news service, a mocked
database engine, a mocked gRPC client), so no network, database or LLM
call happens here. The per-route detail lives in
``test_news_routes_coverage.py``, ``test_health_routes_coverage.py``,
``test_analysis_routes_coverage.py``, ``test_debug_routes_coverage.py``,
``test_backtesting_routes_coverage.py``, ``test_trades_routes_coverage.py``
and ``test_orchestration.py``.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.auth import get_current_user_from_token
from app.main import app
from app.security.rate_limit import limiter

WALLET = "0x" + "ab" * 20


def _authenticate(user_id=1):
    app.dependency_overrides[get_current_user_from_token] = lambda: {
        "user_id": user_id,
        "wallet_address": WALLET,
        "is_admin": False,
    }


class NewsGenerationMaskingTests(unittest.TestCase):
    """Masking + logging on the paid news generation routes."""

    def setUp(self):
        self.service = MagicMock()
        self.service.get_cached_feed.return_value = []
        self.service.get_for_market.return_value = None
        self.service.generate_for_trending = AsyncMock(return_value=[])
        patcher = patch(
            "app.api.routes.news.get_news_service",
            return_value=self.service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Shared in-memory 5/minute budget: clear it so an earlier test
        # cannot 429 this one.
        limiter.reset()
        self.addCleanup(limiter.reset)
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def test_generation_routes_reject_anonymous_callers(self):
        for path, body in (
            ("/api/news/generate", {"condition_id": "c1", "question": "Q?"}),
            ("/api/news/generate/stream", {"condition_id": "c1", "question": "Q?"}),
            ("/api/news/refresh-feed", {}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, json=body)
                self.assertEqual(response.status_code, 401, path)

    def test_generate_serves_authenticated_callers(self):
        _authenticate()
        self.service.generate_for_market = AsyncMock(return_value={"headline": "Generated"})

        response = self.client.post(
            "/api/news/generate",
            json={"condition_id": "c1", "question": "Q?"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["article"]["headline"], "Generated")

    def test_generate_failure_logs_detail_but_returns_generic_message(self):
        _authenticate()
        self.service.generate_for_market = AsyncMock(side_effect=RuntimeError("openai 502"))

        with self.assertLogs("app.api.routes.news", level="ERROR") as captured:
            response = self.client.post(
                "/api/news/generate",
                json={"condition_id": "c1", "question": "Q?"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "News generation failed")
        self.assertNotIn("openai 502", response.text)
        # The provider failure is still recoverable from the log.
        self.assertIn("RuntimeError: openai 502", "\n".join(captured.output))

    def test_generate_stream_error_event_has_no_exception_internals(self):
        _authenticate()
        self.service.generate_for_market = AsyncMock(side_effect=RuntimeError("openai 502"))

        with self.assertLogs("app.api.routes.news", level="ERROR") as captured:
            response = self.client.post(
                "/api/news/generate/stream",
                json={"condition_id": "c1", "question": "Q?"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("event: error", body)
        self.assertIn("News generation failed", body)
        self.assertNotIn("openai 502", body)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("event: done", body)
        self.assertIn("RuntimeError: openai 502", "\n".join(captured.output))

    def test_refresh_feed_failure_returns_generic_message(self):
        _authenticate()
        self.service.generate_for_trending = AsyncMock(side_effect=RuntimeError("feed down"))

        with self.assertLogs("app.api.routes.news", level="ERROR") as captured:
            response = self.client.post("/api/news/refresh-feed", json={})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "News feed refresh failed")
        self.assertNotIn("feed down", response.text)
        self.assertIn("RuntimeError: feed down", "\n".join(captured.output))


class HealthPayloadMaskingTests(unittest.TestCase):
    """``/health`` stays a liveness signal instead of an error oracle."""

    def setUp(self):
        self.addCleanup(app.dependency_overrides.clear)
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

    def _patch_database_down(self):
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError("could not translate host name db")
        self._patch("app.api.routes.health.engine", new=engine)

    def _patch_dependencies_ok(self):
        self._patch("app.api.routes.health._cache_backend", return_value="memory")
        self._patch(
            "app.api.routes.health._grpc_backend_health",
            AsyncMock(return_value={"llm_chain": True, "cli_agent": True}),
        )
        self._patch("app.api.routes.health.scheduler_heartbeat_age", return_value=None)
        self._patch("app.api.routes.health.scheduler_stale_threshold", return_value=300)

    def test_database_failure_payload_has_no_exception_text(self):
        self._patch_database_down()
        self._patch_dependencies_ok()

        with self.assertLogs("app.api.routes.health", level="ERROR") as captured:
            response = self.client.get("/health")

        # 503 and ok=False are preserved; only the message is generic.
        self.assertEqual(response.status_code, 503)
        check = response.json()["checks"]["database"]
        self.assertFalse(check["ok"])
        self.assertEqual(check["error"], "Database check failed")
        self.assertNotIn("could not translate host name", response.text)
        self.assertIn("RuntimeError: could not translate host name db", "\n".join(captured.output))


class DebugCheckMaskingTests(unittest.TestCase):
    """Admin-gated health checks report status without exception text."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_every_check_hides_exception_text(self):
        from app.api.routes.debug import (
            _check_background_tasks,
            _check_inverse_bot_monitor,
            _check_redis,
            _check_research_mcp,
        )

        with (
            patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}),
            patch("redis.from_url", MagicMock(side_effect=RuntimeError("redis down"))),
        ):
            redis_check = self._run(_check_redis())
        self.assertEqual(redis_check.status, "unhealthy")
        self.assertNotIn("redis down", redis_check.detail)

        with patch(
            "app.main._background_tasks",
            MagicMock(side_effect=RuntimeError("bg-probe-secret")),
        ):
            tasks = self._run(_check_background_tasks())
        self.assertNotIn("bg-probe-secret", tasks.detail)

        with patch(
            "app.services.inverse_bot_monitor.get_inverse_bot_metrics",
            MagicMock(side_effect=RuntimeError("metrics-probe-secret")),
        ):
            monitor = self._run(_check_inverse_bot_monitor())
        self.assertNotIn("metrics-probe-secret", monitor.detail)

        with patch(
            "pathlib.Path.exists",
            MagicMock(side_effect=RuntimeError("mcp-probe-secret")),
        ):
            mcp = self._run(_check_research_mcp())
        self.assertNotIn("mcp-probe-secret", mcp.detail)

    def test_check_failures_still_log_the_exception(self):
        from app.api.routes.debug import _check_research_mcp

        with (
            patch("pathlib.Path.exists", MagicMock(side_effect=RuntimeError("mcp-probe-secret"))),
            self.assertLogs("app.api.routes.debug", level="ERROR") as captured,
        ):
            self._run(_check_research_mcp())

        self.assertIn("RuntimeError: mcp-probe-secret", "\n".join(captured.output))


class OrchestrationMaskingTests(unittest.TestCase):
    """The orchestration route masks a backend outage (plan B1.6)."""

    def setUp(self):
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)
        _authenticate()
        patcher = patch("app.api.routes.orchestration._orchestration_llm_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_backend_outage_detail_is_generic(self):
        """A transport failure is logged in full and returned generic.

        Patching ``app.api.routes.orchestration.AnalysisClient`` — the
        symbol the route actually resolves — rather than the class in
        its defining module: the pooled-channel tests around it mutate
        module identity, which is exactly how an earlier version of this
        test silently stopped patching anything.
        """
        with (
            patch(
                "app.api.routes.orchestration.AnalysisClient",
                return_value=SimpleNamespace(
                    orchestrate=AsyncMock(
                        side_effect=RuntimeError("connection refused 10.0.0.5:50051")
                    ),
                    close=AsyncMock(),
                ),
            ),
            self.assertLogs("app.api.routes.orchestration", level="ERROR") as captured,
        ):
            response = self.client.post(
                "/api/analysis/orchestrate",
                json={"intent": "market_deep_dive", "payload": {}},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Orchestration backend unavailable")
        self.assertNotIn("connection refused", response.text)
        self.assertNotIn("10.0.0.5", response.text)
        self.assertIn("RuntimeError: connection refused 10.0.0.5:50051", "\n".join(captured.output))

    def test_stream_emits_the_generic_client_error_event(self):
        from app.grpc_clients.analysis_client import AnalysisClient

        async def _failing_stream(self, **kwargs):
            # What the real client yields when the transport dies: one
            # generic error event, with the exception kept in the log.
            yield {"chunk_type": "error", "error": "Stream processing failed", "is_final": True}

        with (
            patch.object(AnalysisClient, "orchestrate_stream", _failing_stream),
            patch.object(AnalysisClient, "close", AsyncMock()),
        ):
            response = self.client.post(
                "/api/analysis/orchestrate/stream",
                json={"intent": "market_deep_dive", "payload": {}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"error": "Stream processing failed"', response.text)


if __name__ == "__main__":
    unittest.main()
