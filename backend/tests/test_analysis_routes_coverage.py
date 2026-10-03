"""Coverage tests for the analysis API routes (app/api/routes/analysis.py).

Route behaviour is exercised through ``TestClient(app)`` with the auth
dependencies overridden and ``get_analysis_client_for_user`` replaced by
an async factory returning a mock ``AnalysisClient``.  The SSE endpoints
are consumed via ``client.stream(...)``.  The pure helpers
(``get_user_backend``, ``get_analysis_client_for_user``,
``_enrich_trader_request``, ``_group_markets_by_event``) are tested
directly with fake DB sessions, gateway and Polymarket service doubles.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.analysis import (
    _enrich_trader_request,
    _group_markets_by_event,
    get_analysis_client_for_user,
    get_user_backend,
)
from app.api.routes.auth import (
    get_current_user_from_token,
    get_optional_user_from_token,
)
from app.config import AIBackend
from app.grpc_clients.analysis_client import AnalysisClient
from app.main import app
from app.models.user_settings import AIBackendType
from app.services.llm_gateway import LLMConfig, LLMProviderType
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20

MARKET_PAYLOAD = {
    "market_title": "Will BTC hit $150k?",
    "market_description": "Full description",
    "yes_price": 0.6,
    "no_price": 0.4,
    "volume_24h": 1234.5,
    "end_date": "2026-12-31T00:00:00Z",
    "include_research": True,
}

QUICK_PAYLOAD = {"question": "Will it happen?", "current_price": 0.55}

GROUP_PAYLOAD = {
    "event_title": "Election 2028",
    "event_slug": "election-2028",
    "event_volume": 1000.0,
    "event_liquidity": 500.0,
    "sub_markets": [
        {"label": "Democrat", "question": "Democrat wins?", "yes_price": 0.6, "no_price": 0.4},
        {"label": "Republican", "yes_price": 0.4, "no_price": 0.6},
    ],
}

SCAN_PAYLOAD = {"markets": [{"question": "Q1", "condition_id": "c1"}]}

RISK_PAYLOAD = {
    "market_title": "Will BTC hit $150k?",
    "position_size": 100.0,
    "entry_price": 0.5,
    "days_to_expiry": 7,
}

PLAN_PAYLOAD = {
    "action": "buy_yes",
    "market_title": "Will BTC hit $150k?",
    "target_size": 50.0,
    "current_price": 0.55,
    "order_book": {"bids": [[0.5, 10]], "asks": [[0.6, 5]]},
}

TRADER_PAYLOAD = {"wallet_address": WALLET, "display_name": "Trader"}

COPY_PAYLOAD = {
    "trader_wallet": "0x" + "cd" * 20,
    "trader_stats": "70% win rate",
    "market_id": "c1",
    "market_title": "Market",
    "trade_side": "BUY",
    "trade_size": 25.0,
    "current_price": 0.55,
    "user_risk_profile": "max_position_daily_loss",
}

SINGLE_MARKET = {
    "question": "Will it happen?",
    "condition_id": "cid-1",
    "outcomePrices": ["0.6", "0.4"],
    "volume24hr": 100,
    "liquidityNum": 50,
    "end_date_iso": "2026-12-31T00:00:00Z",
    "description": "A market",
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


class AnalysisRouteTests(unittest.TestCase):
    """HTTP-level tests for every analysis route."""

    def setUp(self):
        self.client = TestClient(app)
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": False}
        app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        app.dependency_overrides[get_optional_user_from_token] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: MagicMock()
        self.addCleanup(app.dependency_overrides.clear)

        self.mock_client = self._make_client()
        self.factory = AsyncMock(return_value=(self.mock_client, None))
        factory_patcher = patch(
            "app.api.routes.analysis.get_analysis_client_for_user", self.factory
        )
        factory_patcher.start()
        self.addCleanup(factory_patcher.stop)

    def _make_client(self):
        client = MagicMock()
        client.backend = AIBackend.LLM_CHAIN
        client.close = AsyncMock()
        client.analyze_market = AsyncMock(return_value={"analysis": "full analysis"})
        client.quick_analysis = AsyncMock(return_value={"analysis": "quick analysis"})
        client.quick_group_analysis = AsyncMock(
            return_value={
                "analysis": "group analysis",
                "recommended_option": "Democrat",
                "recommended_side": "YES",
            }
        )
        client.scan_markets = AsyncMock(return_value={"opportunities": []})
        client.assess_risk = AsyncMock(return_value={"risk_rating": "low"})
        client.generate_trade_plan = AsyncMock(return_value={"plan": "steps"})
        client.analyze_trader = AsyncMock(return_value={"analysis": "trader analysis"})
        client.evaluate_copy_trade = AsyncMock(return_value={"analysis": "copy eval"})
        client.health_check = AsyncMock(return_value=True)
        return client

    def _stream(self, path, payload):
        with self.client.stream("POST", path, json=payload) as response:
            status = response.status_code
            body = "\n".join(response.iter_lines())
        return status, body

    # ── POST /api/analysis/market ───────────────────────

    def test_analyze_market_success(self):
        response = self.client.post("/api/analysis/market", json=MARKET_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["analysis"], "full analysis")
        self.assertEqual(data["backend"], "llm_chain")
        self.assertIn("timestamp", data)
        kwargs = self.mock_client.analyze_market.await_args.kwargs
        self.assertEqual(kwargs["market_title"], MARKET_PAYLOAD["market_title"])
        self.assertEqual(kwargs["market_description"], "Full description")
        self.assertEqual(kwargs["yes_price"], 0.6)
        self.assertEqual(kwargs["no_price"], 0.4)
        self.assertEqual(kwargs["volume_24h"], 1234.5)
        self.assertEqual(kwargs["end_date"], "2026-12-31T00:00:00Z")
        self.assertTrue(kwargs["include_research"])
        self.assertIsNone(kwargs["llm_config"])
        self.mock_client.close.assert_awaited_once()

    def test_analyze_market_forwards_llm_config(self):
        self.factory = AsyncMock(return_value=(self.mock_client, {"model": "gpt-4"}))
        with patch("app.api.routes.analysis.get_analysis_client_for_user", self.factory):
            response = self.client.post("/api/analysis/market", json=MARKET_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_client.analyze_market.await_args.kwargs
        self.assertEqual(kwargs["llm_config"], {"model": "gpt-4"})

    def test_analyze_market_error_returns_500(self):
        self.mock_client.analyze_market = AsyncMock(side_effect=RuntimeError("boom"))
        response = self.client.post("/api/analysis/market", json=MARKET_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Analysis failed: boom", response.json()["detail"])

    def test_analyze_market_requires_auth(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        try:
            response = self.client.post("/api/analysis/market", json=MARKET_PAYLOAD)
        finally:
            app.dependency_overrides[get_current_user_from_token] = lambda: self.user
        self.assertEqual(response.status_code, 401)

    def test_analyze_market_validates_price_bounds(self):
        payload = {**MARKET_PAYLOAD, "yes_price": 1.5}
        response = self.client.post("/api/analysis/market", json=payload)
        self.assertEqual(response.status_code, 422)

    # ── POST /api/analysis/market/stream ────────────────

    def test_analyze_market_stream_success(self):
        self.mock_client.analyze_market_stream = MagicMock(
            return_value=_async_gen(["hello", "world"])
        )
        status, body = self._stream("/api/analysis/market/stream", MARKET_PAYLOAD)
        self.assertEqual(status, 200)
        self.assertIn('"chunk": "hello"', body)
        self.assertIn('"chunk": "world"', body)
        self.assertIn('"done": true', body)
        kwargs = self.mock_client.analyze_market_stream.call_args.kwargs
        self.assertEqual(kwargs["market_title"], MARKET_PAYLOAD["market_title"])
        self.mock_client.close.assert_awaited_once()

    def test_analyze_market_stream_forwards_llm_config(self):
        self.factory = AsyncMock(return_value=(self.mock_client, {"provider": 1, "model": "m"}))
        self.mock_client.analyze_market_stream = MagicMock(return_value=_async_gen(["x"]))
        with patch("app.api.routes.analysis.get_analysis_client_for_user", self.factory):
            status, body = self._stream("/api/analysis/market/stream", MARKET_PAYLOAD)
        self.assertEqual(status, 200)
        kwargs = self.mock_client.analyze_market_stream.call_args.kwargs
        self.assertEqual(kwargs["llm_config"], {"provider": 1, "model": "m"})

    def test_analyze_market_stream_error_event(self):
        self.mock_client.analyze_market_stream = MagicMock(
            return_value=_raising_gen(RuntimeError("stream boom"))
        )
        status, body = self._stream("/api/analysis/market/stream", MARKET_PAYLOAD)
        self.assertEqual(status, 200)
        self.assertIn('"error"', body)
        self.assertIn("stream boom", body)
        self.mock_client.close.assert_awaited_once()

    def test_analyze_market_stream_anonymous_user(self):
        app.dependency_overrides[get_optional_user_from_token] = lambda: None
        self.mock_client.analyze_market_stream = MagicMock(return_value=_async_gen(["anon"]))
        try:
            status, body = self._stream("/api/analysis/market/stream", MARKET_PAYLOAD)
        finally:
            app.dependency_overrides[get_optional_user_from_token] = lambda: self.user
        self.assertEqual(status, 200)
        self.assertIn('"chunk": "anon"', body)
        self.assertIsNone(self.factory.await_args.args[0])

    # ── POST /api/analysis/quick ────────────────────────

    def test_quick_analysis_success(self):
        response = self.client.post("/api/analysis/quick", json=QUICK_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["analysis"], "quick analysis")
        self.assertEqual(data["data"]["question"], "Will it happen?")
        self.assertEqual(data["backend"], "llm_chain")
        kwargs = self.mock_client.quick_analysis.await_args.kwargs
        self.assertEqual(kwargs["question"], "Will it happen?")
        self.assertEqual(kwargs["current_price"], 0.55)
        self.mock_client.close.assert_awaited_once()

    def test_quick_analysis_anonymous(self):
        app.dependency_overrides[get_optional_user_from_token] = lambda: None
        try:
            response = self.client.post("/api/analysis/quick", json=QUICK_PAYLOAD)
        finally:
            app.dependency_overrides[get_optional_user_from_token] = lambda: self.user
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.factory.await_args.args[0])

    def test_quick_analysis_error_returns_500(self):
        self.mock_client.quick_analysis = AsyncMock(side_effect=RuntimeError("boom"))
        response = self.client.post("/api/analysis/quick", json=QUICK_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Quick analysis failed: boom", response.json()["detail"])

    # ── POST /api/analysis/quick-group ──────────────────

    def test_quick_group_analysis_success(self):
        response = self.client.post("/api/analysis/quick-group", json=GROUP_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["analysis"], "group analysis")
        self.assertEqual(data["data"]["event_title"], "Election 2028")
        self.assertEqual(data["data"]["recommended_option"], "Democrat")
        self.assertEqual(data["data"]["recommended_side"], "YES")
        kwargs = self.mock_client.quick_group_analysis.await_args.kwargs
        self.assertEqual(kwargs["event_title"], "Election 2028")
        self.assertEqual(kwargs["event_slug"], "election-2028")
        self.assertEqual(kwargs["event_volume"], 1000.0)
        self.assertEqual(kwargs["event_liquidity"], 500.0)
        self.assertEqual(len(kwargs["sub_markets"]), 2)
        self.assertEqual(kwargs["sub_markets"][0]["label"], "Democrat")
        self.assertEqual(kwargs["sub_markets"][0]["question"], "Democrat wins?")
        self.assertEqual(kwargs["sub_markets"][1]["question"], "Republican")
        self.assertEqual(kwargs["sub_markets"][1]["condition_id"], "")
        self.mock_client.close.assert_awaited_once()

    def test_quick_group_analysis_error_returns_500(self):
        self.mock_client.quick_group_analysis = AsyncMock(side_effect=RuntimeError("boom"))
        response = self.client.post("/api/analysis/quick-group", json=GROUP_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Quick group analysis failed: boom", response.json()["detail"])

    def test_quick_group_analysis_requires_two_sub_markets(self):
        payload = {**GROUP_PAYLOAD, "sub_markets": GROUP_PAYLOAD["sub_markets"][:1]}
        response = self.client.post("/api/analysis/quick-group", json=payload)
        self.assertEqual(response.status_code, 422)

    # ── POST /api/analysis/scan ─────────────────────────

    def test_scan_markets_success(self):
        response = self.client.post("/api/analysis/scan", json=SCAN_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["opportunities"], [])
        self.mock_client.scan_markets.assert_awaited_once()
        self.assertEqual(
            self.mock_client.scan_markets.await_args.kwargs["markets"],
            SCAN_PAYLOAD["markets"],
        )
        self.mock_client.close.assert_awaited_once()

    def test_scan_markets_rejects_more_than_20(self):
        payload = {"markets": [{"question": f"Q{i}"} for i in range(21)]}
        response = self.client.post("/api/analysis/scan", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Maximum 20 markets", response.json()["detail"])
        self.mock_client.scan_markets.assert_not_awaited()

    def test_scan_markets_error_returns_500(self):
        self.mock_client.scan_markets = AsyncMock(side_effect=RuntimeError("boom"))
        response = self.client.post("/api/analysis/scan", json=SCAN_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Market scan failed: boom", response.json()["detail"])

    # ── POST /api/analysis/risk ─────────────────────────

    def test_assess_risk_success_without_correlation(self):
        response = self.client.post("/api/analysis/risk", json=RISK_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["risk_rating"], "low")
        kwargs = self.mock_client.assess_risk.await_args.kwargs
        self.assertEqual(kwargs["market_title"], RISK_PAYLOAD["market_title"])
        self.assertEqual(kwargs["position_size"], 100.0)
        self.assertEqual(kwargs["entry_price"], 0.5)
        self.assertEqual(kwargs["days_to_expiry"], 7)
        self.assertEqual(kwargs["correlation_info"], "No correlation data")
        self.mock_client.close.assert_awaited_once()

    def test_assess_risk_with_correlation_info(self):
        payload = {**RISK_PAYLOAD, "correlation_info": "correlated with X"}
        response = self.client.post("/api/analysis/risk", json=payload)
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_client.assess_risk.await_args.kwargs
        self.assertEqual(kwargs["correlation_info"], "correlated with X")

    def test_assess_risk_error_returns_500(self):
        self.mock_client.assess_risk = AsyncMock(side_effect=RuntimeError("boom"))
        response = self.client.post("/api/analysis/risk", json=RISK_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Risk assessment failed: boom", response.json()["detail"])

    # ── POST /api/analysis/trade-plan ─────────────────────

    def test_generate_trade_plan_success(self):
        response = self.client.post("/api/analysis/trade-plan", json=PLAN_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["plan"], "steps")
        kwargs = self.mock_client.generate_trade_plan.await_args.kwargs
        self.assertEqual(kwargs["action"], "buy_yes")
        self.assertEqual(kwargs["target_size"], 50.0)
        self.assertEqual(kwargs["current_price"], 0.55)
        self.assertEqual(kwargs["order_book"], PLAN_PAYLOAD["order_book"])
        self.mock_client.close.assert_awaited_once()

    def test_generate_trade_plan_without_order_book(self):
        payload = {k: v for k, v in PLAN_PAYLOAD.items() if k != "order_book"}
        response = self.client.post("/api/analysis/trade-plan", json=payload)
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_client.generate_trade_plan.await_args.kwargs
        self.assertIsNone(kwargs["order_book"])

    def test_generate_trade_plan_rejects_invalid_action(self):
        payload = {**PLAN_PAYLOAD, "action": "hold"}
        response = self.client.post("/api/analysis/trade-plan", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_generate_trade_plan_error_returns_500(self):
        self.mock_client.generate_trade_plan = AsyncMock(side_effect=RuntimeError("boom"))
        response = self.client.post("/api/analysis/trade-plan", json=PLAN_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Trade plan generation failed: boom", response.json()["detail"])

    # ── POST /api/analysis/trader ────────────────────────

    def test_analyze_trader_success(self):
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(
                return_value={
                    "stats": {
                        "win_rate": 0.7,
                        "total_trades": 10,
                        "unique_markets": 5,
                        "total_volume": 1000.0,
                        "total_pnl": 250.0,
                    },
                    "trade_summary": "real trades",
                }
            ),
        ):
            response = self.client.post("/api/analysis/trader", json=TRADER_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["analysis"], "trader analysis")
        kwargs = self.mock_client.analyze_trader.await_args.kwargs
        self.assertEqual(kwargs["wallet_address"], WALLET)
        self.assertEqual(kwargs["display_name"], "Trader")
        self.assertEqual(kwargs["win_rate"], 0.7)
        self.assertEqual(kwargs["trade_count"], 10)
        self.assertEqual(kwargs["markets_traded"], 5)
        self.assertEqual(kwargs["total_pnl"], 250.0)
        self.assertEqual(kwargs["recent_trades_json"], "real trades")
        self.assertEqual(kwargs["user_id"], "1")
        self.mock_client.close.assert_awaited_once()

    def test_analyze_trader_error_returns_500(self):
        self.mock_client.analyze_trader = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(return_value={"stats": {}, "trade_summary": ""}),
        ):
            response = self.client.post("/api/analysis/trader", json=TRADER_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Trader analysis failed: boom", response.json()["detail"])

    # ── POST /api/analysis/trader/stream ────────────────

    def test_analyze_trader_stream_success(self):
        self.mock_client.analyze_trader_stream = MagicMock(
            return_value=_async_gen(["trader chunk"])
        )
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(return_value={"stats": {}, "trade_summary": "sum"}),
        ):
            status, body = self._stream("/api/analysis/trader/stream", TRADER_PAYLOAD)
        self.assertEqual(status, 200)
        # initial empty status chunk, the analysis chunk, then done
        self.assertIn('"chunk": ""', body)
        self.assertIn('"chunk": "trader chunk"', body)
        self.assertIn('"done": true', body)
        self.mock_client.close.assert_awaited_once()

    def test_analyze_trader_stream_error_event(self):
        self.mock_client.analyze_trader_stream = MagicMock(
            return_value=_raising_gen(RuntimeError("trader boom"))
        )
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(return_value={"stats": {}, "trade_summary": ""}),
        ):
            status, body = self._stream("/api/analysis/trader/stream", TRADER_PAYLOAD)
        self.assertEqual(status, 200)
        self.assertIn('"error"', body)
        self.assertIn("trader boom", body)

    # ── POST /api/analysis/copy-trade-eval ────────────────

    def test_evaluate_copy_trade_success(self):
        response = self.client.post("/api/analysis/copy-trade-eval", json=COPY_PAYLOAD)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["analysis"], "copy eval")
        kwargs = self.mock_client.evaluate_copy_trade.await_args.kwargs
        self.assertEqual(kwargs["trader_wallet"], COPY_PAYLOAD["trader_wallet"])
        self.assertEqual(kwargs["trader_stats"], "70% win rate")
        self.assertEqual(kwargs["trade_side"], "BUY")
        self.assertEqual(kwargs["trade_size"], 25.0)
        self.assertEqual(kwargs["current_price"], 0.55)
        self.assertEqual(kwargs["user_risk_profile"], "max_position_daily_loss")
        self.assertEqual(kwargs["user_id"], "1")
        self.mock_client.close.assert_awaited_once()

    def test_evaluate_copy_trade_rejects_invalid_side(self):
        payload = {**COPY_PAYLOAD, "trade_side": "HOLD"}
        response = self.client.post("/api/analysis/copy-trade-eval", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_evaluate_copy_trade_error_returns_500(self):
        self.mock_client.evaluate_copy_trade = AsyncMock(side_effect=RuntimeError("boom"))
        response = self.client.post("/api/analysis/copy-trade-eval", json=COPY_PAYLOAD)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Copy trade evaluation failed: boom", response.json()["detail"])

    # ── GET /api/analysis/health ────────────────────────

    def test_health_check_route(self):
        instance = MagicMock()
        instance.health_check = AsyncMock(return_value=True)
        instance.close = AsyncMock()
        with patch(
            "app.grpc_clients.analysis_client.AnalysisClient", return_value=instance
        ) as client_cls:
            response = self.client.get("/api/analysis/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertTrue(data["backends"]["llm_chain"]["healthy"])
        self.assertTrue(data["backends"]["cli_agent"]["healthy"])
        self.assertEqual(
            data["backends"]["llm_chain"]["host"],
            "localhost:50051",
        )
        self.assertEqual(
            data["backends"]["cli_agent"]["host"],
            "localhost:50052",
        )
        self.assertEqual(client_cls.call_count, 2)
        self.assertEqual(instance.health_check.await_count, 2)
        self.assertEqual(instance.close.await_count, 2)

    def test_health_check_route_unhealthy(self):
        instance = MagicMock()
        instance.health_check = AsyncMock(return_value=False)
        instance.close = AsyncMock()
        with patch("app.grpc_clients.analysis_client.AnalysisClient", return_value=instance):
            response = self.client.get("/api/analysis/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "unhealthy")
        self.assertFalse(data["backends"]["llm_chain"]["healthy"])

    # ── POST /api/analysis/opportunities/analyze-markets ──

    def _patch_service(self, service):
        patcher = patch("app.api.routes.analysis.get_polymarket_service", return_value=service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_analyze_markets_stream_empty(self):
        self._patch_service(MagicMock())
        status, body = self._stream("/api/analysis/opportunities/analyze-markets", {"markets": []})
        self.assertEqual(status, 200)
        self.assertIn('"all_done": true', body)
        self.assertIn('"total": 0', body)

    def test_analyze_markets_stream_single_market(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(
            return_value={
                "whale_activity": "whales buying",
                "trader_stats": "stats",
                "market_signal": "bullish",
            }
        )
        self._patch_service(service)
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={
                "condition_id": "cid-1",
                "market_title": "Will it happen?",
                "ai_score": 70,
                "risk_level": "low",
                "pnl_potential": 0.2,
                "credibility_score": 80,
                "smart_money_signal": "bullish",
                "smart_money_summary": "sum",
                "recommendation": "buy",
                "recommended_side": "YES",
                "reasoning": "r",
                "search_summary": "s",
                "key_risks": [],
            }
        )
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": [SINGLE_MARKET]}
        )
        self.assertEqual(status, 200)
        self.assertIn('"status": "analyzing"', body)
        self.assertIn('"total": 1', body)
        self.assertIn('"market_id": "cid-1"', body)
        self.assertIn('"all_done": true', body)
        service.get_smart_money_analysis.assert_awaited_once_with("cid-1")
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(kwargs["market_title"], "Will it happen?")
        self.assertEqual(kwargs["condition_id"], "cid-1")
        self.assertEqual(kwargs["yes_price"], 0.6)
        self.assertEqual(kwargs["no_price"], 0.4)
        self.assertEqual(kwargs["volume_24h"], 100.0)
        self.assertEqual(kwargs["liquidity"], 50.0)
        self.assertEqual(kwargs["end_date"], "2026-12-31T00:00:00Z")
        self.assertEqual(
            kwargs["smart_money_context"],
            "Whale activity: whales buying\nTrader stats: stats\nSignal: bullish",
        )

    def test_analyze_markets_stream_smart_money_empty(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": [SINGLE_MARKET]}
        )
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(kwargs["smart_money_context"], "No smart-money data")

    def test_analyze_markets_stream_smart_money_error(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(side_effect=RuntimeError("net"))
        self._patch_service(service)
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": [SINGLE_MARKET]}
        )
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(kwargs["smart_money_context"], "Smart-money data unavailable")

    def test_analyze_markets_stream_multi_market_event(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)
        self.mock_client.scan_event_opportunity = AsyncMock(
            return_value={
                "event_title": "event-1",
                "ai_score": 60,
                "risk_level": "medium",
                "pnl_potential": 0.5,
                "credibility_score": 50,
                "smart_money_signal": "neutral",
                "smart_money_summary": "",
                "recommendation": "hold",
                "recommended_side": "YES",
                "recommended_option": "Democrat",
                "reasoning": "r",
                "search_summary": "",
                "key_risks": [],
            }
        )
        markets = [
            {
                **SINGLE_MARKET,
                "question": "Q1",
                "condition_id": "c1",
                "slug": "event-1",
                "_event_title": "Event One",
                "groupItemTitle": "Opt A",
            },
            {
                "question": "Q2",
                "condition_id": "c2",
                "slug": "event-1",
                "_event_title": "Event One",
                "outcomePrices": ["0.3", "0.7"],
                "groupItemTitle": "Opt B",
            },
        ]
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": markets}
        )
        self.assertEqual(status, 200)
        # both sub-markets receive the event-level scores
        self.assertIn('"market_id": "c1"', body)
        self.assertIn('"market_id": "c2"', body)
        self.assertIn('"all_done": true', body)
        self.assertIn('"total": 2', body)
        kwargs = self.mock_client.scan_event_opportunity.await_args.kwargs
        self.assertEqual(kwargs["event_title"], "Event One")
        self.assertEqual(len(kwargs["sub_markets"]), 2)
        self.assertEqual(kwargs["sub_markets"][0]["label"], "Opt A")
        self.assertEqual(kwargs["sub_markets"][0]["yes_price"], 0.6)
        self.assertEqual(kwargs["sub_markets"][1]["label"], "Opt B")
        self.assertEqual(kwargs["sub_markets"][1]["yes_price"], 0.3)
        self.assertEqual(kwargs["sub_markets"][1]["no_price"], 0.7)

    def test_analyze_markets_stream_multi_market_bad_prices(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)
        self.mock_client.scan_event_opportunity = AsyncMock(
            return_value={
                "event_title": "Event One",
                "ai_score": 60,
                "risk_level": "medium",
                "pnl_potential": 0.5,
                "credibility_score": 50,
                "smart_money_signal": "neutral",
                "smart_money_summary": "",
                "recommendation": "hold",
                "recommended_side": "YES",
                "recommended_option": "",
                "reasoning": "r",
                "search_summary": "",
                "key_risks": [],
            }
        )
        markets = [
            {
                **SINGLE_MARKET,
                "condition_id": "c1",
                "slug": "event-1",
                "_event_title": "Event One",
                "outcomePrices": ["bad"],
            },
            {
                "question": "Q2",
                "condition_id": "c2",
                "slug": "event-1",
                "_event_title": "Event One",
                "outcomePrices": ["also-bad", "worse"],
            },
        ]
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": markets}
        )
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_event_opportunity.await_args.kwargs
        # unparseable prices fall back to 0.5/0.5
        self.assertEqual(kwargs["sub_markets"][0]["yes_price"], 0.5)
        self.assertEqual(kwargs["sub_markets"][0]["no_price"], 0.5)
        self.assertEqual(kwargs["sub_markets"][1]["yes_price"], 0.5)
        self.assertEqual(kwargs["sub_markets"][1]["no_price"], 0.5)

    def test_analyze_markets_stream_slow_scan(self):
        # covers the TimeoutError → continue branch of the result loop
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)

        async def _slow_scan(**kwargs):
            await asyncio.sleep(2.2)
            return {"condition_id": "cid-1", "ai_score": 55}

        self.mock_client.scan_opportunity = AsyncMock(side_effect=_slow_scan)
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": [SINGLE_MARKET]}
        )
        self.assertEqual(status, 200)
        self.assertIn('"ai_score": 55', body)
        self.assertIn('"all_done": true', body)

    def test_analyze_markets_stream_non_serializable_scores(self):
        # json.dumps failure in the result loop → error event; the
        # still-running second task is cancelled in the finally block
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)

        async def _scan(**kwargs):
            if kwargs.get("condition_id") == "cid-1":
                return {"condition_id": "cid-1", "ai_score": object()}
            await asyncio.sleep(2.2)
            return {"condition_id": "cid-2", "ai_score": 50}

        self.mock_client.scan_opportunity = AsyncMock(side_effect=_scan)
        markets = [
            SINGLE_MARKET,
            {**SINGLE_MARKET, "question": "Q2", "condition_id": "cid-2"},
        ]
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": markets}
        )
        self.assertEqual(status, 200)
        self.assertIn('"error"', body)

    def test_analyze_markets_stream_bad_volume_defaults(self):
        # an unparseable volume24hr/liquidity no longer kills the
        # analysis task: the market is still scanned with the
        # volume/liquidity defaulting to 0
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        market = {**SINGLE_MARKET, "volume24hr": "bad"}
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": [market]}
        )
        self.assertEqual(status, 200)
        self.assertIn('"all_done": true', body)
        self.assertIn('"total": 1', body)
        self.assertIn('"market_id": "cid-1"', body)
        # scan_opportunity received the defaulted volume/liquidity
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(kwargs["volume_24h"], 0.0)
        self.assertEqual(kwargs["liquidity"], 0.0)

    def test_analyze_markets_stream_scan_opportunity_error(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)
        self.mock_client.scan_opportunity = AsyncMock(side_effect=RuntimeError("ai down"))
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": [SINGLE_MARKET]}
        )
        self.assertEqual(status, 200)
        self.assertIn('"market_id": "cid-1"', body)
        self.assertIn('"ai_score": 30', body)
        self.assertIn('"risk_level": "high"', body)
        self.assertIn("AI analysis failed: ai down", body)

    def test_analyze_markets_stream_scan_event_error(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)
        self.mock_client.scan_event_opportunity = AsyncMock(side_effect=RuntimeError("ai down"))
        markets = [
            {**SINGLE_MARKET, "condition_id": "c1", "slug": "event-1"},
            {"question": "Q2", "condition_id": "c2", "slug": "event-1"},
        ]
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": markets}
        )
        self.assertEqual(status, 200)
        self.assertIn('"ai_score": 30', body)
        self.assertIn("AI analysis failed: ai down", body)

    def test_analyze_markets_stream_bad_prices_fall_back(self):
        service = MagicMock()
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self._patch_service(service)
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        market = {**SINGLE_MARKET, "outcomePrices": ["bad", "also-bad"]}
        status, body = self._stream(
            "/api/analysis/opportunities/analyze-markets", {"markets": [market]}
        )
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(kwargs["yes_price"], 0.5)
        self.assertEqual(kwargs["no_price"], 0.5)

    # ── POST /api/analysis/opportunities/stream ─────────

    def test_opportunity_stream_serves_from_cache(self):
        cache = MagicMock()
        cache.get.return_value = [
            {"condition_id": "c1", "ai_score": 80},
            {"condition_id": "c2", "ai_score": 70},
        ]
        with patch("app.api.routes.analysis.opportunity_cache", cache):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn('"cached": true', body)
        self.assertIn('"market_id": "c1"', body)
        self.assertIn('"done_count": 1', body)
        self.assertIn('"done_count": 2', body)
        self.assertIn('"total": 2', body)
        cache.set.assert_not_called()

    def test_opportunity_stream_force_refresh_bypasses_cache(self):
        cache = MagicMock()
        cache.get.return_value = [{"condition_id": "c1", "ai_score": 80}]
        service = MagicMock()
        service.get_newest_markets = AsyncMock(return_value=[])
        service.get_active_markets = AsyncMock(return_value=[])
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream(
                "/api/analysis/opportunities/stream",
                {"limit": 10, "force_refresh": True},
            )
        self.assertEqual(status, 200)
        cache.get.assert_not_called()
        self.assertIn("No markets found", body)

    def test_opportunity_stream_full_flow(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(
            return_value=[
                {**SINGLE_MARKET, "slug": "event-1"},
                {
                    "question": "Q2",
                    "condition_id": "c2",
                    "slug": "event-1",
                    "outcomePrices": ["0.3", "0.7"],
                },
            ]
        )
        service.get_active_markets = AsyncMock(
            return_value=[
                # duplicate of the first newest market → deduplicated
                {**SINGLE_MARKET, "slug": "event-1"},
                {"question": "Solo", "condition_id": "c3", "slug": "solo-1"},
            ]
        )
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self.mock_client.scan_event_opportunity = AsyncMock(
            return_value={
                "event_title": "event-1",
                "ai_score": 60,
                "risk_level": "medium",
                "pnl_potential": 0.5,
                "credibility_score": 50,
                "smart_money_signal": "neutral",
                "smart_money_summary": "",
                "recommendation": "hold",
                "recommended_side": "YES",
                "recommended_option": "",
                "reasoning": "r",
                "search_summary": "",
                "key_risks": [],
            }
        )
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={
                "condition_id": "c3",
                "market_title": "Solo",
                "ai_score": 90,
                "risk_level": "low",
                "pnl_potential": 0.9,
                "credibility_score": 95,
                "smart_money_signal": "bullish",
                "smart_money_summary": "s",
                "recommendation": "strong_buy",
                "recommended_side": "YES",
                "reasoning": "r",
                "search_summary": "",
                "key_risks": [],
            }
        )
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 40})
        self.assertEqual(status, 200)
        self.assertIn('"status": "fetching_markets"', body)
        self.assertIn('"status": "markets_loaded"', body)
        self.assertIn('"total": 3', body)
        # dedup: cid-1 appears once even though it was in both feeds
        self.assertEqual(body.count('"market_id": "cid-1"'), 1)
        self.assertIn('"market_id": "c2"', body)
        self.assertIn('"market_id": "c3"', body)
        self.assertIn('"all_done": true', body)
        # results sorted by ai_score descending and cached for 20 minutes
        cache.set.assert_called_once()
        args, kwargs = cache.set.call_args
        self.assertEqual(kwargs.get("ttl_seconds"), 1200)
        cached_results = args[1]
        self.assertEqual([r["ai_score"] for r in cached_results], [90, 60, 60])
        service.get_newest_markets.assert_awaited_once_with(limit=40)
        service.get_active_markets.assert_awaited_once_with(limit=40)

    def test_opportunity_stream_no_markets(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(return_value=[])
        service.get_active_markets = AsyncMock(return_value=[])
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn("No markets found", body)

    def test_opportunity_stream_fetch_error(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(side_effect=RuntimeError("fetch down"))
        service.get_active_markets = AsyncMock(return_value=[])
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn("Failed to fetch markets", body)
        self.assertIn("fetch down", body)

    def test_opportunity_stream_smart_money_full(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(return_value=[SINGLE_MARKET])
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(
            return_value={
                "whale_activity": "whales buying",
                "trader_stats": "stats",
                "market_signal": "bullish",
            }
        )
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(
            kwargs["smart_money_context"],
            "Whale activity: whales buying\nTrader stats: stats\nSignal: bullish",
        )

    def test_opportunity_stream_smart_money_error(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(return_value=[SINGLE_MARKET])
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(side_effect=RuntimeError("net down"))
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(kwargs["smart_money_context"], "Smart-money data unavailable")

    def test_opportunity_stream_single_market_bad_prices(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(
            return_value=[{**SINGLE_MARKET, "outcomePrices": ["bad"]}]
        )
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_opportunity.await_args.kwargs
        self.assertEqual(kwargs["yes_price"], 0.5)
        self.assertEqual(kwargs["no_price"], 0.5)

    def test_opportunity_stream_multi_market_bad_prices(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(
            return_value=[
                {
                    **SINGLE_MARKET,
                    "condition_id": "c1",
                    "slug": "event-1",
                    "outcomePrices": ["bad"],
                },
                {
                    "question": "Q2",
                    "condition_id": "c2",
                    "slug": "event-1",
                    "outcomePrices": ["also-bad", "worse"],
                },
            ]
        )
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self.mock_client.scan_event_opportunity = AsyncMock(
            return_value={
                "event_title": "event-1",
                "ai_score": 60,
                "risk_level": "medium",
                "pnl_potential": 0.5,
                "credibility_score": 50,
                "smart_money_signal": "neutral",
                "smart_money_summary": "",
                "recommendation": "hold",
                "recommended_side": "YES",
                "recommended_option": "",
                "reasoning": "r",
                "search_summary": "",
                "key_risks": [],
            }
        )
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        kwargs = self.mock_client.scan_event_opportunity.await_args.kwargs
        self.assertEqual(kwargs["sub_markets"][0]["yes_price"], 0.5)
        self.assertEqual(kwargs["sub_markets"][0]["no_price"], 0.5)
        self.assertEqual(kwargs["sub_markets"][1]["yes_price"], 0.5)
        self.assertEqual(kwargs["sub_markets"][1]["no_price"], 0.5)

    def test_opportunity_stream_non_serializable_scores(self):
        # json.dumps failure in the result loop → error event; the
        # still-running second task is cancelled in the finally block
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(
            return_value=[
                SINGLE_MARKET,
                {**SINGLE_MARKET, "question": "Q2", "condition_id": "cid-2"},
            ]
        )
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(return_value={})

        async def _scan(**kwargs):
            if kwargs.get("condition_id") == "cid-1":
                return {"condition_id": "cid-1", "ai_score": object()}
            await asyncio.sleep(2.2)
            return {"condition_id": "cid-2", "ai_score": 50}

        self.mock_client.scan_opportunity = AsyncMock(side_effect=_scan)
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn('"error"', body)

    def test_opportunity_stream_scan_opportunity_error(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(return_value=[SINGLE_MARKET])
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self.mock_client.scan_opportunity = AsyncMock(side_effect=RuntimeError("ai down"))
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn('"ai_score": 30', body)
        self.assertIn("AI analysis failed: ai down", body)
        cache.set.assert_called_once()

    def test_opportunity_stream_scan_event_error(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(
            return_value=[
                {**SINGLE_MARKET, "condition_id": "c1", "slug": "event-1"},
                {"question": "Q2", "condition_id": "c2", "slug": "event-1"},
            ]
        )
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self.mock_client.scan_event_opportunity = AsyncMock(side_effect=RuntimeError("ai down"))
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn('"ai_score": 30', body)
        self.assertIn("AI analysis failed: ai down", body)

    def test_opportunity_stream_markets_without_condition_id_skipped(self):
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(return_value=[{"question": "No ID"}])
        service.get_active_markets = AsyncMock(return_value=[])
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn("No markets found", body)

    def test_opportunity_stream_slow_scan_retries_then_completes(self):
        # covers the TimeoutError → continue branch of the result loop
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(return_value=[SINGLE_MARKET])
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(return_value={})

        async def _slow_scan(**kwargs):
            await asyncio.sleep(2.2)
            return {"condition_id": "cid-1", "ai_score": 55}

        self.mock_client.scan_opportunity = AsyncMock(side_effect=_slow_scan)
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn('"ai_score": 55', body)
        self.assertIn('"all_done": true', body)

    def test_opportunity_stream_bad_volume_defaults(self):
        # an unparseable volume24hr no longer kills the analysis
        # task: the market is still scanned with volume defaulting
        # to 0
        cache = MagicMock()
        cache.get.return_value = None
        service = MagicMock()
        service.get_newest_markets = AsyncMock(
            return_value=[{**SINGLE_MARKET, "volume24hr": "bad"}]
        )
        service.get_active_markets = AsyncMock(return_value=[])
        service.get_smart_money_analysis = AsyncMock(return_value={})
        self.mock_client.scan_opportunity = AsyncMock(
            return_value={"condition_id": "cid-1", "ai_score": 50}
        )
        with (
            patch("app.api.routes.analysis.opportunity_cache", cache),
            patch("app.api.routes.analysis.get_polymarket_service", return_value=service),
        ):
            status, body = self._stream("/api/analysis/opportunities/stream", {"limit": 10})
        self.assertEqual(status, 200)
        self.assertIn('"all_done": true', body)
        self.assertIn('"total": 1', body)
        self.assertIn('"market_id": "cid-1"', body)


class AnalysisRouteHelperTests(unittest.IsolatedAsyncioTestCase):
    """Direct tests for the route module's helper functions."""

    def setUp(self):
        self.user = {"user_id": 1, "wallet_address": WALLET, "is_admin": False}

    # ── get_user_backend ────────────────────────────────

    async def test_get_user_backend_none_user(self):
        self.assertEqual(await get_user_backend(None, MagicMock()), AIBackend.LLM_CHAIN)

    async def test_get_user_backend_cli_agent_preference(self):
        record = MagicMock()
        record.ai_backend = AIBackendType.CLI_AGENT.value
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = record
        self.assertEqual(await get_user_backend(1, db), AIBackend.CLI_AGENT)

    async def test_get_user_backend_llm_chain_preference(self):
        record = MagicMock()
        record.ai_backend = AIBackendType.LLM_CHAIN.value
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = record
        self.assertEqual(await get_user_backend(1, db), AIBackend.LLM_CHAIN)

    async def test_get_user_backend_no_settings_record(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        self.assertEqual(await get_user_backend(1, db), AIBackend.LLM_CHAIN)

    # ── get_analysis_client_for_user ────────────────────

    async def test_get_analysis_client_for_user_anonymous(self):
        gateway = MagicMock()
        gateway.resolve_provider.return_value = (AIBackend.LLM_CHAIN, None)
        with patch("app.api.routes.analysis.get_llm_gateway", return_value=gateway):
            client, config = await get_analysis_client_for_user(None, MagicMock())
        self.assertIsInstance(client, AnalysisClient)
        self.assertEqual(client.backend, AIBackend.LLM_CHAIN)
        self.assertIsNone(config)
        gateway.resolve_provider.assert_called_once_with(user_settings=None)

    async def test_get_analysis_client_for_user_with_llm_config(self):
        record = MagicMock()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = record
        gateway = MagicMock()
        config = LLMConfig(
            provider=LLMProviderType.OPENAI,
            model="gpt-4o",
            temperature=0.5,
            max_tokens=100,
        )
        gateway.resolve_provider.return_value = (AIBackend.CLI_AGENT, config)
        with patch("app.api.routes.analysis.get_llm_gateway", return_value=gateway):
            client, llm_config = await get_analysis_client_for_user(1, db)
        self.assertIsInstance(client, AnalysisClient)
        self.assertEqual(client.backend, AIBackend.CLI_AGENT)
        self.assertEqual(llm_config["provider"], 1)
        self.assertEqual(llm_config["model"], "gpt-4o")
        self.assertEqual(llm_config["temperature"], 0.5)
        self.assertEqual(llm_config["max_tokens"], 100)
        gateway.resolve_provider.assert_called_once_with(user_settings=record)

    async def test_get_analysis_client_for_user_no_config(self):
        record = MagicMock()
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = record
        gateway = MagicMock()
        gateway.resolve_provider.return_value = (AIBackend.LLM_CHAIN, None)
        with patch("app.api.routes.analysis.get_llm_gateway", return_value=gateway):
            client, llm_config = await get_analysis_client_for_user(1, db)
        self.assertIsNone(llm_config)

    # ── _enrich_trader_request ──────────────────────────

    async def test_enrich_trader_request_skips_fetch(self):
        from app.api.routes.analysis import TraderAnalysisRequest

        request = TraderAnalysisRequest(
            wallet_address=WALLET,
            display_name="Trader",
            total_pnl=10.0,
            win_rate=0.5,
            trade_count=3,
            markets_traded=2,
            recent_trades_json="[{}]",
            fetch_real_trades=False,
        )
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(return_value={"stats": {}, "trade_summary": "x"}),
        ) as fetch:
            enriched = await _enrich_trader_request(request)
        fetch.assert_not_awaited()
        self.assertEqual(enriched["wallet_address"], WALLET)
        self.assertEqual(enriched["display_name"], "Trader")
        self.assertEqual(enriched["total_pnl"], 10.0)
        self.assertEqual(enriched["win_rate"], 0.5)
        self.assertEqual(enriched["trade_count"], 3)
        self.assertEqual(enriched["markets_traded"], 2)
        self.assertEqual(enriched["recent_trades_json"], "[{}]")

    async def test_enrich_trader_request_applies_real_stats(self):
        from app.api.routes.analysis import TraderAnalysisRequest

        request = TraderAnalysisRequest(
            wallet_address=WALLET, fetch_real_trades=True, total_pnl=0.0
        )
        trade_data = {
            "stats": {
                "win_rate": 0.7,
                "total_trades": 10,
                "unique_markets": 5,
                "total_volume": 1000.0,
                "total_pnl": 250.0,
            },
            "trade_summary": "real trades",
        }
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(return_value=trade_data),
        ):
            enriched = await _enrich_trader_request(request)
        self.assertEqual(enriched["win_rate"], 0.7)
        self.assertEqual(enriched["trade_count"], 10)
        self.assertEqual(enriched["markets_traded"], 5)
        self.assertEqual(enriched["total_pnl"], 250.0)
        self.assertEqual(enriched["recent_trades_json"], "real trades")

    async def test_enrich_trader_request_zero_stats_keep_request_values(self):
        from app.api.routes.analysis import TraderAnalysisRequest

        request = TraderAnalysisRequest(
            wallet_address=WALLET,
            display_name="T",
            total_pnl=5.0,
            win_rate=0.4,
            trade_count=2,
            markets_traded=1,
            fetch_real_trades=True,
        )
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(return_value={"stats": {}, "trade_summary": "sum"}),
        ):
            enriched = await _enrich_trader_request(request)
        self.assertEqual(enriched["win_rate"], 0.4)
        self.assertEqual(enriched["trade_count"], 2)
        self.assertEqual(enriched["markets_traded"], 1)
        # total_volume is 0 → total_pnl not overridden
        self.assertEqual(enriched["total_pnl"], 5.0)
        self.assertEqual(enriched["recent_trades_json"], "sum")

    async def test_enrich_trader_request_pnl_fallback_without_total_pnl(self):
        from app.api.routes.analysis import TraderAnalysisRequest

        request = TraderAnalysisRequest(
            wallet_address=WALLET, fetch_real_trades=True, total_pnl=0.0
        )
        # total_volume > 0 but stats carry no total_pnl key
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(
                return_value={
                    "stats": {"total_volume": 500.0},
                    "trade_summary": "sum",
                }
            ),
        ):
            enriched = await _enrich_trader_request(request)
        self.assertEqual(enriched["total_pnl"], 0.0)

    async def test_enrich_trader_request_fetch_failure_falls_back(self):
        from app.api.routes.analysis import TraderAnalysisRequest

        request = TraderAnalysisRequest(
            wallet_address=WALLET,
            display_name="T",
            total_pnl=5.0,
            win_rate=0.4,
            trade_count=2,
            markets_traded=1,
            recent_trades_json="[{}]",
            fetch_real_trades=True,
        )
        with patch(
            "app.api.routes.analysis.fetch_trader_trades",
            AsyncMock(side_effect=RuntimeError("network down")),
        ):
            enriched = await _enrich_trader_request(request)
        self.assertEqual(enriched["win_rate"], 0.4)
        self.assertEqual(enriched["trade_count"], 2)
        self.assertEqual(enriched["recent_trades_json"], "[{}]")

    # ── _group_markets_by_event ─────────────────────────

    def test_group_markets_by_event(self):
        markets = [
            {
                "_event_slug": "s1",
                "_event_title": "Event One",
                "_event_volume": "100",
                "_event_liquidity": "50",
                "question": "Q1",
            },
            {"_event_slug": "s1", "question": "Q2"},
            {"slug": "s2", "question": "Q3", "volume": "10", "liquidity": "5"},
            {"question": "Solo question"},
        ]
        groups = _group_markets_by_event(markets)
        self.assertEqual(len(groups), 3)

        first = groups[0]
        self.assertEqual(first["slug"], "s1")
        self.assertEqual(first["title"], "Event One")
        self.assertEqual(first["volume"], "100")
        self.assertEqual(first["liquidity"], "50")
        self.assertEqual(len(first["markets"]), 2)
        self.assertEqual(first["markets"][0]["question"], "Q1")
        self.assertEqual(first["markets"][1]["question"], "Q2")

        second = groups[1]
        self.assertEqual(second["slug"], "s2")
        self.assertEqual(second["title"], "Q3")
        self.assertEqual(second["volume"], "10")
        self.assertEqual(second["liquidity"], "5")
        self.assertEqual(len(second["markets"]), 1)

        solo = groups[2]
        self.assertEqual(solo["slug"], "Solo question")
        self.assertEqual(solo["title"], "Solo question")
        self.assertEqual(solo["volume"], "0")
        self.assertEqual(solo["liquidity"], "0")
        self.assertEqual(len(solo["markets"]), 1)

    def test_group_markets_by_event_empty(self):
        self.assertEqual(_group_markets_by_event([]), [])


if __name__ == "__main__":
    unittest.main()
