"""Coverage for the news routes (``/api/news``).

Exercises the cached feed, per-market article lookup,
on-demand generation (success and failure), the SSE
streaming endpoint (progress, article, done and error
events), feed refresh, request validation, and the SSE
event formatter. The news service singleton is replaced
with a mock so no LLM or cache I/O happens.

The three generation/refresh POST routes are
authenticated and rate limited (plan B1): the tests
authenticate through a dependency override, and the
auth checks themselves are exercised with the override
absent.
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.news import _sse_event
from app.main import app
from app.security.rate_limit import limiter


def _fake_service():
    svc = MagicMock()
    svc.get_cached_feed.return_value = [
        {"condition_id": "c1", "headline": "First"},
        {"condition_id": "c2", "headline": "Second"},
    ]
    svc.get_for_market.return_value = {
        "condition_id": "c1",
        "headline": "First",
    }
    svc.generate_for_market = AsyncMock(
        return_value={"condition_id": "c1", "headline": "Generated"}
    )
    svc.generate_for_trending = AsyncMock(
        return_value=[{"condition_id": "c2", "headline": "Refreshed"}]
    )
    return svc


class NewsRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.service = _fake_service()
        patcher = patch(
            "app.api.routes.news.get_news_service",
            return_value=self.service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The news POST routes carry a 5/minute per-IP budget; the counter
        # lives in the shared in-memory limiter, so it is cleared between
        # tests instead of letting an earlier test 429 the next one.
        limiter.reset()
        self.addCleanup(limiter.reset)
        self._authenticate()
        self.client = TestClient(app)

    def _authenticate(self, user_id=1):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": user_id,
            "wallet_address": "0x" + "ab" * 20,
            "is_admin": False,
        }
        self.addCleanup(app.dependency_overrides.pop, get_current_user_from_token, None)


class FeedRouteTests(NewsRouteTestCase):
    def test_feed_returns_cached_articles(self):
        response = self.client.get("/api/news/feed")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["articles"][0]["condition_id"], "c1")
        self.service.get_cached_feed.assert_called_once_with(limit=20)

    def test_feed_forwards_limit(self):
        response = self.client.get("/api/news/feed?limit=5")
        self.assertEqual(response.status_code, 200)
        self.service.get_cached_feed.assert_called_once_with(limit=5)

    def test_feed_validates_limit(self):
        for query in ("limit=0", "limit=51"):
            response = self.client.get(f"/api/news/feed?{query}")
            self.assertEqual(response.status_code, 422, query)


class MarketNewsRouteTests(NewsRouteTestCase):
    def test_market_news_returns_cached_article(self):
        response = self.client.get("/api/news/market/c1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["cached"])
        self.assertEqual(body["article"]["condition_id"], "c1")
        self.service.get_for_market.assert_called_once_with("c1")

    def test_market_news_reports_miss(self):
        self.service.get_for_market.return_value = None
        response = self.client.get("/api/news/market/unknown")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["cached"])
        self.assertIsNone(body["article"])


class GenerateRouteTests(NewsRouteTestCase):
    def test_generate_requires_condition_id_and_question(self):
        response = self.client.post("/api/news/generate", json={})
        self.assertEqual(response.status_code, 422)

    def test_generate_returns_article(self):
        response = self.client.post(
            "/api/news/generate",
            json={
                "condition_id": "c1",
                "question": "Will BTC go up?",
                "force": True,
                "provider": "openai",
                "model": "gpt-4o",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["cached"])
        self.assertEqual(body["article"]["headline"], "Generated")
        self.service.generate_for_market.assert_awaited_once_with(
            condition_id="c1",
            question="Will BTC go up?",
            force=True,
            provider="openai",
            model="gpt-4o",
        )

    def test_generate_defaults_optional_fields(self):
        response = self.client.post(
            "/api/news/generate",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 200)
        self.service.generate_for_market.assert_awaited_once_with(
            condition_id="c1",
            question="Q?",
            force=False,
            provider=None,
            model=None,
        )

    def test_generate_failure_returns_502(self):
        self.service.generate_for_market = AsyncMock(side_effect=RuntimeError("llm down"))
        response = self.client.post(
            "/api/news/generate",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 502)
        # The provider failure text stays in the log, never in the body.
        self.assertEqual(response.json()["detail"], "News generation failed")
        self.assertNotIn("llm down", response.text)

    def test_generate_is_rate_limited_below_the_global_budget(self):
        body = {"condition_id": "c1", "question": "Q?"}

        for _ in range(5):
            self.assertEqual(self.client.post("/api/news/generate", json=body).status_code, 200)

        # The sixth call inside a minute is the endpoint's own 5/minute cap,
        # not the global 100/minute one.
        sixth = self.client.post("/api/news/generate", json=body)
        self.assertEqual(sixth.status_code, 429)


class GenerateStreamRouteTests(NewsRouteTestCase):
    def test_stream_emits_status_article_and_done(self):
        response = self.client.post(
            "/api/news/generate/stream",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        text = response.text
        self.assertIn("event: status", text)
        self.assertIn("event: article", text)
        self.assertIn("event: done", text)
        self.assertIn("Generation complete", text)
        self.service.generate_for_market.assert_awaited_once()

    def test_stream_emits_error_event_on_failure(self):
        self.service.generate_for_market = AsyncMock(side_effect=RuntimeError("llm down"))
        response = self.client.post(
            "/api/news/generate/stream",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("event: status", text)
        self.assertIn("event: error", text)
        self.assertIn("News generation failed", text)
        # No exception internals cross the SSE boundary.
        self.assertNotIn("llm down", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("event: done", text)


class RefreshFeedRouteTests(NewsRouteTestCase):
    def test_refresh_feed_regenerates_trending(self):
        response = self.client.post(
            "/api/news/refresh-feed",
            json={"max_markets": 10, "provider": "openai", "model": "gpt-4o"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["articles"][0]["condition_id"], "c2")
        self.service.generate_for_trending.assert_awaited_once_with(
            max_markets=10,
            provider="openai",
            model="gpt-4o",
        )

    def test_refresh_feed_defaults(self):
        response = self.client.post("/api/news/refresh-feed", json={})
        self.assertEqual(response.status_code, 200)
        self.service.generate_for_trending.assert_awaited_once_with(
            max_markets=5,
            provider=None,
            model=None,
        )

    def test_refresh_feed_validates_max_markets(self):
        for body in ({"max_markets": 0}, {"max_markets": 21}):
            response = self.client.post("/api/news/refresh-feed", json=body)
            self.assertEqual(response.status_code, 422, body)

    def test_refresh_feed_failure_returns_502(self):
        self.service.generate_for_trending = AsyncMock(side_effect=RuntimeError("trending down"))
        response = self.client.post("/api/news/refresh-feed", json={})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "News feed refresh failed")
        self.assertNotIn("trending down", response.text)


class GenerationAuthTests(unittest.TestCase):
    """The paid-generation POST routes are authenticated; reads are not."""

    def setUp(self):
        limiter.reset()
        self.addCleanup(limiter.reset)
        patcher = patch(
            "app.api.routes.news.get_news_service",
            return_value=_fake_service(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def test_generate_requires_authentication(self):
        response = self.client.post(
            "/api/news/generate",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 401)

    def test_generate_stream_requires_authentication(self):
        response = self.client.post(
            "/api/news/generate/stream",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 401)

    def test_refresh_feed_requires_authentication(self):
        response = self.client.post("/api/news/refresh-feed", json={})
        self.assertEqual(response.status_code, 401)

    def test_invalid_credentials_are_rejected(self):
        app.dependency_overrides[get_current_user_from_token] = lambda: (_ for _ in ()).throw(
            HTTPException(status_code=401, detail="Invalid authentication credentials")
        )
        response = self.client.post(
            "/api/news/generate",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 401)

    def test_authenticated_request_still_generates(self):
        app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 7}
        response = self.client.post(
            "/api/news/generate",
            json={"condition_id": "c1", "question": "Q?"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["article"]["headline"], "Generated")

    def test_read_routes_stay_public(self):
        for path in ("/api/news/feed", "/api/news/market/c1"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)


class SseEventHelperTests(unittest.TestCase):
    def test_sse_event_format(self):
        event = _sse_event("status", {"message": "hi"})
        self.assertEqual(event, 'event: status\ndata: {"message": "hi"}\n\n')

    def test_sse_event_serializes_non_ascii(self):
        event = _sse_event("done", {"message": "café"})
        payload = event.split("data: ", 1)[1].strip()
        self.assertEqual(json.loads(payload), {"message": "café"})


if __name__ == "__main__":
    unittest.main()
