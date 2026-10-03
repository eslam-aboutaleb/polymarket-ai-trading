"""Coverage tests for the markets routes (app/api/routes/markets.py).

The Polymarket service and the gRPC ``AnalysisClient`` are replaced
with mocks so the public browse/search endpoints and the SSE
trader-analysis stream are exercised without network access.
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.auth import (
    get_current_user_from_token,
    get_optional_user_from_token,
)
from app.api.routes.markets import _get_analysis_client
from app.config import AIBackend
from app.grpc_clients.analysis_client import AnalysisClient
from app.main import app
from app.models.user_settings import AIBackendType
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20

TRADER_STATS = {
    "total_trades": 100,
    "yes_traders": 30,
    "yes_volume": 1000.0,
    "no_traders": 20,
    "no_volume": 500.0,
    "side_ratio": {"yes": 66.7, "no": 33.3},
    "top_traders": [
        {
            "short_address": "0xab..cd",
            "total_volume": 250.0,
            "yes_volume": 200.0,
            "no_volume": 50.0,
            "lean": "YES",
        },
    ],
}

STREAM_PAYLOAD = {
    "condition_id": "0xabc123",
    "question": "Will BTC hit $150k?",
    "yes_price": 0.6,
    "no_price": 0.4,
    "volume_24h": 1234.5,
    "end_date": "2026-12-31T00:00:00Z",
}


def _async_gen(items):
    async def gen():
        for item in items:
            yield item

    return gen()


def _raising_gen(error):
    async def gen():
        raise error
        yield  # pragma: no cover - makes this an async generator

    return gen()


def _fake_settings_db(record=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = record
    return db


class GetAnalysisClientTests(unittest.TestCase):
    """Direct tests for the _get_analysis_client helper."""

    def setUp(self):
        self.addCleanup(app.dependency_overrides.clear)

    def test_anonymous_user_gets_llm_chain(self):
        client = asyncio_result(_get_analysis_client(None, MagicMock()))
        self.assertIsInstance(client, AnalysisClient)
        self.assertEqual(client.backend, AIBackend.LLM_CHAIN)

    def test_user_with_cli_agent_backend(self):
        record = MagicMock()
        record.ai_backend = AIBackendType.CLI_AGENT.value
        client = asyncio_result(
            _get_analysis_client(1, _fake_settings_db(record)),
        )
        self.assertEqual(client.backend, AIBackend.CLI_AGENT)

    def test_user_with_llm_chain_backend(self):
        record = MagicMock()
        record.ai_backend = AIBackendType.LLM_CHAIN.value
        client = asyncio_result(
            _get_analysis_client(1, _fake_settings_db(record)),
        )
        self.assertEqual(client.backend, AIBackend.LLM_CHAIN)

    def test_user_without_settings_record(self):
        client = asyncio_result(
            _get_analysis_client(1, _fake_settings_db(None)),
        )
        self.assertEqual(client.backend, AIBackend.LLM_CHAIN)

    def test_user_settings_queried_by_user_id(self):
        db = _fake_settings_db(None)
        asyncio_result(_get_analysis_client(42, db))
        db.query.assert_called_once()
        filter_call = db.query.return_value.filter.call_args
        self.assertIsNotNone(filter_call)


class CategoriesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.service = MagicMock()
        self.service.get_market_categories = MagicMock(
            return_value=[{"tag": "crypto"}, {"tag": "politics"}],
        )
        patcher = patch(
            "app.api.routes.markets.get_polymarket_service",
            lambda: self.service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_categories(self):
        response = self.client.get("/api/markets/categories")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"categories": [{"tag": "crypto"}, {"tag": "politics"}]},
        )
        self.service.get_market_categories.assert_called_once()

    def test_empty_categories(self):
        self.service.get_market_categories = MagicMock(return_value=[])
        response = self.client.get("/api/markets/categories")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"categories": []})


class SearchMarketsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.service = MagicMock()
        self.service.search_all_markets = AsyncMock(
            return_value={"markets": [{"id": "m1"}], "count": 1},
        )
        patcher = patch(
            "app.api.routes.markets.get_polymarket_service",
            lambda: self.service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_search_with_defaults(self):
        response = self.client.get("/api/markets/search")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"markets": [{"id": "m1"}], "count": 1})
        kwargs = self.service.search_all_markets.await_args.kwargs
        self.assertEqual(kwargs["query"], "")
        self.assertEqual(kwargs["tag"], "")
        self.assertEqual(kwargs["limit"], 20)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["sort"], "volume24hr")

    def test_search_with_params(self):
        response = self.client.get(
            "/api/markets/search",
            params={
                "q": "bitcoin",
                "tag": "crypto",
                "limit": 10,
                "offset": 5,
                "sort": "liquidity",
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.service.search_all_markets.await_args.kwargs
        self.assertEqual(kwargs["query"], "bitcoin")
        self.assertEqual(kwargs["tag"], "crypto")
        self.assertEqual(kwargs["limit"], 10)
        self.assertEqual(kwargs["offset"], 5)
        self.assertEqual(kwargs["sort"], "liquidity")

    def test_search_limit_validation(self):
        for bad in (0, 101):
            response = self.client.get(
                "/api/markets/search",
                params={"limit": bad},
            )
            self.assertEqual(response.status_code, 422)

    def test_search_offset_validation(self):
        response = self.client.get(
            "/api/markets/search",
            params={"offset": -1},
        )
        self.assertEqual(response.status_code, 422)


class BrowseMarketsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.service = MagicMock()
        self.service.get_markets_by_category = AsyncMock(
            return_value=[{"id": "m1"}, {"id": "m2"}],
        )
        patcher = patch(
            "app.api.routes.markets.get_polymarket_service",
            lambda: self.service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_browse_requires_tag(self):
        response = self.client.get("/api/markets/browse")
        self.assertEqual(response.status_code, 422)

    def test_browse_returns_markets(self):
        response = self.client.get(
            "/api/markets/browse",
            params={"tag": "crypto"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"tag": "crypto", "markets": [{"id": "m1"}, {"id": "m2"}], "count": 2},
        )
        kwargs = self.service.get_markets_by_category.await_args.kwargs
        self.assertEqual(kwargs["tag"], "crypto")
        self.assertEqual(kwargs["limit"], 60)

    def test_browse_with_limit(self):
        response = self.client.get(
            "/api/markets/browse",
            params={"tag": "crypto", "limit": 5},
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.service.get_markets_by_category.await_args.kwargs
        self.assertEqual(kwargs["limit"], 5)

    def test_browse_empty_result(self):
        self.service.get_markets_by_category = AsyncMock(return_value=[])
        response = self.client.get(
            "/api/markets/browse",
            params={"tag": "empty"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"tag": "empty", "markets": [], "count": 0},
        )

    def test_browse_limit_validation(self):
        for bad in (0, 101):
            response = self.client.get(
                "/api/markets/browse",
                params={"tag": "crypto", "limit": bad},
            )
            self.assertEqual(response.status_code, 422)


class TraderAnalysisStreamTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": False}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_optional_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: MagicMock()
        self.addCleanup(app.dependency_overrides.clear)

        self.service = MagicMock()
        self.service.get_market_trader_stats = AsyncMock(
            return_value=TRADER_STATS,
        )
        self.mock_client = MagicMock()
        self.mock_client.close = AsyncMock()
        self.mock_client.analyze_market_stream = MagicMock(
            return_value=_async_gen(["chunk1", "chunk2"]),
        )

        service_patcher = patch(
            "app.api.routes.markets.get_polymarket_service",
            lambda: self.service,
        )
        service_patcher.start()
        self.addCleanup(service_patcher.stop)

        self.client_factory = AsyncMock(return_value=self.mock_client)
        client_patcher = patch(
            "app.api.routes.markets._get_analysis_client",
            self.client_factory,
        )
        client_patcher.start()
        self.addCleanup(client_patcher.stop)

    def _stream(self, payload):
        with self.client.stream(
            "POST",
            "/api/markets/trader-analysis/stream",
            json=payload,
        ) as response:
            status = response.status_code
            body = "\n".join(response.iter_lines())
        return status, body

    def test_stream_success(self):
        status, body = self._stream(STREAM_PAYLOAD)
        self.assertEqual(status, 200)
        # First event carries the trader stats
        self.assertIn('"trader_stats"', body)
        self.assertIn('"total_trades": 100', body)
        self.assertIn('"yes_traders": 30', body)
        self.assertIn('"no_volume": 500.0', body)
        self.assertIn('"side_ratio"', body)
        self.assertIn('"top_traders"', body)
        # Then the streamed chunks
        self.assertIn('"chunk": "chunk1"', body)
        self.assertIn('"chunk": "chunk2"', body)
        self.assertIn('"done": true', body)
        # The analysis client received the enriched prompt
        kwargs = self.mock_client.analyze_market_stream.call_args.kwargs
        self.assertEqual(kwargs["market_title"], STREAM_PAYLOAD["question"])
        self.assertEqual(kwargs["yes_price"], 0.6)
        self.assertEqual(kwargs["no_price"], 0.4)
        self.assertEqual(kwargs["volume_24h"], 1234.5)
        self.assertEqual(kwargs["end_date"], "2026-12-31T00:00:00Z")
        self.assertTrue(kwargs["include_research"])
        self.assertIn("Will BTC hit $150k?", kwargs["market_description"])
        self.assertIn("TRADER POSITIONING DATA", kwargs["market_description"])
        self.assertIn("0xab..cd", kwargs["market_description"])
        self.mock_client.close.assert_awaited_once()

    def test_stream_stats_event_shape(self):
        status, body = self._stream(STREAM_PAYLOAD)
        self.assertEqual(status, 200)
        first_line = body.split("\n")[0]
        self.assertTrue(first_line.startswith("data: "))
        payload = json.loads(first_line[len("data: ") :])
        stats = payload["trader_stats"]
        self.assertEqual(stats["total_trades"], 100)
        self.assertEqual(stats["yes_traders"], 30)
        self.assertEqual(stats["no_traders"], 20)
        self.assertEqual(stats["yes_volume"], 1000.0)
        self.assertEqual(stats["no_volume"], 500.0)
        self.assertEqual(stats["side_ratio"], {"yes": 66.7, "no": 33.3})
        self.assertEqual(len(stats["top_traders"]), 1)

    def test_stream_error_event(self):
        self.mock_client.analyze_market_stream = MagicMock(
            return_value=_raising_gen(RuntimeError("stream boom")),
        )
        status, body = self._stream(STREAM_PAYLOAD)
        self.assertEqual(status, 200)
        self.assertIn('"trader_stats"', body)
        self.assertIn('"error"', body)
        self.assertIn("stream boom", body)
        self.assertNotIn('"done": true', body)
        self.mock_client.close.assert_awaited_once()

    def test_stream_anonymous_user(self):
        app.dependency_overrides[get_optional_user_from_token] = lambda: None
        try:
            status, body = self._stream(STREAM_PAYLOAD)
        finally:
            app.dependency_overrides[get_optional_user_from_token] = lambda: self.user
        self.assertEqual(status, 200)
        self.assertIn('"chunk": "chunk1"', body)
        self.assertIsNone(self.client_factory.await_args.args[0])

    def test_stream_end_date_defaults_to_now(self):
        payload = dict(STREAM_PAYLOAD.items())
        payload["end_date"] = ""
        status, body = self._stream(payload)
        self.assertEqual(status, 200)
        kwargs = self.mock_client.analyze_market_stream.call_args.kwargs
        self.assertTrue(kwargs["end_date"])
        self.assertNotEqual(kwargs["end_date"], "")

    def test_stream_top_traders_sliced_to_eight(self):
        stats = dict(TRADER_STATS)
        stats["top_traders"] = [
            {
                "short_address": f"0x{i:02x}..{i:02x}",
                "total_volume": float(i),
                "yes_volume": float(i),
                "no_volume": 0.0,
                "lean": "YES",
            }
            for i in range(12)
        ]
        self.service.get_market_trader_stats = AsyncMock(return_value=stats)
        status, body = self._stream(STREAM_PAYLOAD)
        self.assertEqual(status, 200)
        kwargs = self.mock_client.analyze_market_stream.call_args.kwargs
        # Only the first 8 traders are included in the prompt
        self.assertEqual(kwargs["market_description"].count("•"), 8)
        # The stats event slices to 6
        first_line = body.split("\n")[0]
        payload = json.loads(first_line[len("data: ") :])
        self.assertEqual(len(payload["trader_stats"]["top_traders"]), 6)

    def test_stream_requires_condition_id(self):
        payload = dict(STREAM_PAYLOAD.items())
        del payload["condition_id"]
        response = self.client.post(
            "/api/markets/trader-analysis/stream",
            json=payload,
        )
        self.assertEqual(response.status_code, 422)

    def test_stream_requires_question(self):
        payload = dict(STREAM_PAYLOAD.items())
        del payload["question"]
        response = self.client.post(
            "/api/markets/trader-analysis/stream",
            json=payload,
        )
        self.assertEqual(response.status_code, 422)

    def test_stream_validates_price_bounds(self):
        for field in ("yes_price", "no_price"):
            payload = dict(STREAM_PAYLOAD.items())
            payload[field] = 1.5
            response = self.client.post(
                "/api/markets/trader-analysis/stream",
                json=payload,
            )
            self.assertEqual(response.status_code, 422)

    def test_stream_response_headers(self):
        with self.client.stream(
            "POST",
            "/api/markets/trader-analysis/stream",
            json=STREAM_PAYLOAD,
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers["content-type"].split(";")[0],
                "text/event-stream",
            )
            self.assertEqual(response.headers["cache-control"], "no-cache")
            self.assertEqual(response.headers["connection"], "keep-alive")
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            # Consume the body so the generator finishes
            _ = "\n".join(response.iter_lines())


def asyncio_result(coro):
    """Run a coroutine to completion on a fresh event loop."""
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    unittest.main()
