"""Coverage tests for the request-logging middleware.

Complements ``test_request_logger_sanitization.py`` (which covers
query sanitization and IP hashing) by exercising the remaining
public helpers and every ``RequestLogMiddleware.dispatch`` branch:

* ``get_endpoint_stats`` / ``get_uptime_seconds`` accessors.
* ``_classify_level`` -- error / warning / info branches.
* ``_hash_client_ip`` -- empty / "unknown" / None inputs.
* ``dispatch`` -- 5xx responses (error level + error_detail +
  error_count), exceptions re-raised through the middleware,
  min/max/total duration aggregation, repeated 4xx warning
  counts, missing client info, user-agent truncation, query
  sanitization, the noise-prefix skip list and newest-first
  log ordering.
"""

import asyncio
import unittest
from unittest.mock import MagicMock

from starlette.requests import Request
from starlette.responses import Response

import app.middleware.request_logger as request_logger
from app.middleware.request_logger import (
    RequestLogMiddleware,
    _classify_level,
    _hash_client_ip,
    clear_logs,
    get_endpoint_stats,
    get_log_entries,
    get_uptime_seconds,
)


def _make_request(
    path="/api/items",
    method="GET",
    query="",
    user_agent="test-agent/1.0",
    client=("127.0.0.1", 5000),
):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"user-agent", user_agent.encode("utf-8"))],
        "query_string": query.encode("utf-8") if query else b"",
    }
    if client is not None:
        scope["client"] = client
    return Request(scope)


class RequestLoggerHelperTests(unittest.TestCase):
    def setUp(self):
        clear_logs()
        request_logger._endpoint_stats.clear()

    def tearDown(self):
        clear_logs()
        request_logger._endpoint_stats.clear()

    def test_classify_level_branches(self):
        self.assertEqual(_classify_level(500), "error")
        self.assertEqual(_classify_level(503), "error")
        self.assertEqual(_classify_level(400), "warning")
        self.assertEqual(_classify_level(404), "warning")
        self.assertEqual(_classify_level(200), "info")
        self.assertEqual(_classify_level(302), "info")

    def test_hash_client_ip_unknown_values(self):
        self.assertEqual(_hash_client_ip(""), "unknown")
        self.assertEqual(_hash_client_ip("unknown"), "unknown")
        self.assertEqual(_hash_client_ip(None), "unknown")

    def test_get_endpoint_stats_empty(self):
        self.assertEqual(get_endpoint_stats(), {})

    def test_get_uptime_seconds_non_negative(self):
        self.assertGreaterEqual(get_uptime_seconds(), 0.0)


class RequestLogMiddlewareDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_logs()
        request_logger._endpoint_stats.clear()
        self.middleware = RequestLogMiddleware(MagicMock())

    def tearDown(self):
        clear_logs()
        request_logger._endpoint_stats.clear()

    async def test_dispatch_500_response_logged_as_error(self):
        response = Response(status_code=500)

        async def call_next(request):
            return response

        result = await self.middleware.dispatch(_make_request(), call_next)

        self.assertIs(result, response)
        entry = get_log_entries()[0]
        self.assertEqual(entry["status_code"], 500)
        self.assertEqual(entry["level"], "error")
        self.assertEqual(entry["error_detail"], "internal_server_error")
        stats = get_endpoint_stats()["GET /api/items"]
        self.assertEqual(stats["call_count"], 1)
        self.assertEqual(stats["error_count"], 1)
        self.assertEqual(stats["warning_count"], 0)
        self.assertEqual(stats["last_status"], 500)

    async def test_dispatch_exception_is_reraised_and_logged(self):
        async def call_next(request):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await self.middleware.dispatch(_make_request(), call_next)

        entry = get_log_entries()[0]
        self.assertEqual(entry["status_code"], 500)
        self.assertEqual(entry["level"], "error")
        self.assertEqual(entry["error_detail"], "internal_server_error")
        stats = get_endpoint_stats()["GET /api/items"]
        self.assertEqual(stats["error_count"], 1)

    async def test_dispatch_updates_min_and_max_duration(self):
        async def fast(request):
            return Response(status_code=200)

        async def slow(request):
            await asyncio.sleep(0.1)
            return Response(status_code=200)

        await self.middleware.dispatch(_make_request(path="/api/timing"), fast)
        await self.middleware.dispatch(_make_request(path="/api/timing"), slow)

        stats = get_endpoint_stats()["GET /api/timing"]
        self.assertEqual(stats["call_count"], 2)
        self.assertLess(stats["min_duration_ms"], stats["max_duration_ms"])
        self.assertGreaterEqual(stats["total_duration_ms"], stats["max_duration_ms"])

    async def test_dispatch_increments_warning_count_on_repeat_4xx(self):
        async def call_next(request):
            return Response(status_code=404)

        await self.middleware.dispatch(_make_request(path="/api/missing"), call_next)
        await self.middleware.dispatch(_make_request(path="/api/missing"), call_next)

        stats = get_endpoint_stats()["GET /api/missing"]
        self.assertEqual(stats["call_count"], 2)
        self.assertEqual(stats["warning_count"], 2)
        self.assertEqual(stats["error_count"], 0)

    async def test_dispatch_without_client_ip(self):
        async def call_next(request):
            return Response(status_code=200)

        await self.middleware.dispatch(_make_request(client=None), call_next)

        entry = get_log_entries()[0]
        self.assertEqual(entry["client_ip"], "unknown")

    async def test_dispatch_truncates_long_user_agent(self):
        async def call_next(request):
            return Response(status_code=200)

        await self.middleware.dispatch(_make_request(user_agent="a" * 300), call_next)

        entry = get_log_entries()[0]
        self.assertEqual(entry["user_agent"], "a" * 120)

    async def test_dispatch_sanitizes_query_string(self):
        async def call_next(request):
            return Response(status_code=200)

        await self.middleware.dispatch(_make_request(query="token=abc&limit=10"), call_next)

        entry = get_log_entries()[0]
        self.assertIn("token=%2A%2A%2A", entry["query"])
        self.assertIn("limit=10", entry["query"])

    async def test_dispatch_skips_noise_prefixes(self):
        calls = []

        async def call_next(request):
            calls.append(request.url.path)
            return Response(status_code=200)

        for path in (
            "/health",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/favicon.ico",
            "/docs/oauth2-redirect",
        ):
            await self.middleware.dispatch(_make_request(path=path), call_next)

        self.assertEqual(
            calls,
            [
                "/health",
                "/docs",
                "/openapi.json",
                "/redoc",
                "/favicon.ico",
                "/docs/oauth2-redirect",
            ],
        )
        self.assertEqual(len(get_log_entries()), 0)
        self.assertEqual(get_endpoint_stats(), {})

    async def test_dispatch_post_201_is_info_without_error_detail(self):
        async def call_next(request):
            return Response(status_code=201)

        await self.middleware.dispatch(_make_request(method="POST", path="/api/items"), call_next)

        entry = get_log_entries()[0]
        self.assertEqual(entry["method"], "POST")
        self.assertEqual(entry["status_code"], 201)
        self.assertEqual(entry["level"], "info")
        self.assertNotIn("error_detail", entry)
        self.assertIn("POST /api/items", get_endpoint_stats())

    async def test_log_entries_are_newest_first(self):
        async def call_next(request):
            return Response(status_code=200)

        await self.middleware.dispatch(_make_request(path="/api/first"), call_next)
        await self.middleware.dispatch(_make_request(path="/api/second"), call_next)

        entries = get_log_entries()
        self.assertEqual([e["path"] for e in entries], ["/api/second", "/api/first"])


if __name__ == "__main__":
    unittest.main()
