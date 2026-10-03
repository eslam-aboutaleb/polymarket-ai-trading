"""Coverage tests for the Analysis gRPC client (app/grpc_clients/analysis_client.py).

The real gRPC transport is replaced with a fake channel/stub pair:
``grpc.aio.insecure_channel`` returns a MagicMock channel and
``AnalysisServiceStub`` returns a MagicMock stub whose RPC methods are
AsyncMocks returning real proto response messages.  This exercises every
public method, every branch (success, gRPC error, service-level failure,
invalid payloads) and the module-level helpers (prompt loading, LLM config
building, channel pooling, shared-channel shutdown).
"""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import grpc

import app.grpc_clients.analysis_client as analysis_client
from app.config import AIBackend, get_settings
from app.grpc_clients import analysis_pb2


class _FakeRpcError(grpc.RpcError):
    """RpcError double exposing code()/details()."""

    def __init__(self, code=grpc.StatusCode.UNAVAILABLE, details="backend down"):
        super().__init__()
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


def _ok_response(analysis="analysis text", metadata=None):
    return analysis_pb2.AnalysisResponse(
        success=True,
        analysis=analysis,
        research_context="research-context",
        timestamp="2026-01-01T00:00:00Z",
        metadata=metadata if metadata is not None else {"key": "value"},
    )


def _news_response():
    return analysis_pb2.GenerateNewsResponse(
        success=True,
        headline="Headline",
        summary="Summary",
        body="Body text",
        sentiment="bullish",
        confidence=0.75,
        key_insights=["insight-1", "insight-2"],
        trader_behavior_summary="trader-behaviour",
        market_outlook="market-outlook",
        tags=["tag-a", "tag-b"],
        market_question="Will it happen?",
        condition_id="0xcond-1",
        generated_at="2026-01-01T00:00:00Z",
        metadata={"k": "v"},
    )


def _async_stream(chunks):
    async def gen():
        for c in chunks:
            yield analysis_pb2.AnalysisChunk(chunk=c)

    return gen()


class AnalysisClientCoverageTests(unittest.IsolatedAsyncioTestCase):
    """Base fixture: fake pooled channel + stub, real proto messages."""

    async def asyncSetUp(self):
        self.mod = analysis_client
        # Reset module-level pooling state so each test starts clean and
        # the lazy lock is bound to this test's event loop.
        await self.mod.close_shared_channels()
        self.mod._shared_channels_lock = None

        self.channel = MagicMock()
        self.channel.get_state.return_value = grpc.ChannelConnectivity.IDLE
        self.channel.close = AsyncMock()

        self.stub = MagicMock()
        self._install_default_stub_responses()

        channel_patcher = patch.object(
            self.mod.grpc.aio,
            "insecure_channel",
            return_value=self.channel,
        )
        channel_patcher.start()
        self.addCleanup(channel_patcher.stop)

        stub_patcher = patch(
            "app.grpc_clients.analysis_pb2_grpc.AnalysisServiceStub",
            side_effect=lambda channel: self.stub,
        )
        stub_patcher.start()
        self.addCleanup(stub_patcher.stop)

        self.client = self.mod.AnalysisClient()

    async def asyncTearDown(self):
        await self.mod.close_shared_channels()
        self.mod._shared_channels_lock = None

    def _install_default_stub_responses(self):
        ok = _ok_response()
        for name in (
            "AnalyzeMarket",
            "QuickAnalysis",
            "ScanMarkets",
            "AssessRisk",
            "GenerateTradePlan",
            "AnalyzeTrader",
            "EvaluateCopyTrade",
            "EvaluateInversePosition",
            "AnalyzeSentiment",
            "DiscoverBestTrade",
            "IndexMarketsRAG",
        ):
            setattr(self.stub, name, AsyncMock(return_value=ok))
        self.stub.HealthCheck = AsyncMock(return_value=analysis_pb2.HealthResponse(healthy=True))
        self.stub.GenerateNews = AsyncMock(return_value=_news_response())
        self.stub.GenerateNewsBatch = AsyncMock(
            return_value=analysis_pb2.GenerateNewsBatchResponse(
                success=True, articles=[_news_response()]
            )
        )
        self.stub.AnalyzeMarketStream = MagicMock(
            side_effect=lambda request, timeout=None: _async_stream(["chunk-one", "chunk-two"])
        )

    # ── module-level helpers ──────────────────────────────────

    def test_get_channels_lock_creates_and_reuses(self):
        self.mod._shared_channels_lock = None
        lock = self.mod._get_channels_lock()
        self.assertIsNotNone(lock)
        self.assertIs(lock, self.mod._get_channels_lock())

    def test_load_prompts_reads_real_file_and_caches(self):
        self.mod._load_prompts.cache_clear()
        try:
            prompts = self.mod._load_prompts()
            self.assertIsInstance(prompts, dict)
            self.assertIn("opportunity_analysis", prompts)
            self.assertIn("easy_trade_analysis", prompts)
            # lru_cache: second call returns the identical object
            self.assertIs(self.mod._load_prompts(), prompts)
        finally:
            self.mod._load_prompts.cache_clear()

    def test_load_prompts_returns_empty_dict_when_unreadable(self):
        self.mod._load_prompts.cache_clear()
        try:
            with patch("app.grpc_clients.analysis_client.Path") as mock_path:
                mock_path.return_value.open.side_effect = OSError("no file")
                self.assertEqual(self.mod._load_prompts(), {})
        finally:
            self.mod._load_prompts.cache_clear()

    def test_build_llm_config_none_and_empty(self):
        self.assertIsNone(self.mod._build_llm_config(None))
        self.assertIsNone(self.mod._build_llm_config({}))

    def test_build_llm_config_full(self):
        config = self.mod._build_llm_config(
            {"provider": 2, "model": "gpt-4", "temperature": 0.7, "max_tokens": 500}
        )
        self.assertIsNotNone(config)
        self.assertEqual(config.provider, 2)
        self.assertEqual(config.model, "gpt-4")
        self.assertAlmostEqual(config.temperature, 0.7)
        self.assertEqual(config.max_tokens, 500)

    def test_build_llm_config_partial_and_falsy_fields(self):
        config = self.mod._build_llm_config({"model": "claude"})
        self.assertEqual(config.model, "claude")
        self.assertEqual(config.provider, 0)  # proto default

        config = self.mod._build_llm_config(
            {"provider": 1, "model": "", "temperature": 0, "max_tokens": 0}
        )
        # falsy values are skipped, proto defaults remain
        self.assertEqual(config.provider, 1)
        self.assertEqual(config.model, "")
        self.assertEqual(config.temperature, 0.0)
        self.assertEqual(config.max_tokens, 0)

    def test_build_llm_config_proto_unavailable(self):
        with patch("app.grpc_clients.analysis_pb2.LLMConfig", side_effect=AttributeError):
            self.assertIsNone(self.mod._build_llm_config({"model": "x"}))

    # ── construction / addressing ─────────────────────────────

    def test_init_defaults_to_settings_backend(self):
        client = self.mod.AnalysisClient()
        self.assertEqual(client.backend, get_settings().default_ai_backend)
        self.assertIsNone(client._channel)
        self.assertIsNone(client._stub)

    def test_service_address_per_backend(self):
        settings = get_settings()
        llm = self.mod.AnalysisClient(backend=AIBackend.LLM_CHAIN)
        self.assertEqual(
            llm.service_address,
            f"{settings.llm_chain_grpc_host}:{settings.llm_chain_grpc_port}",
        )
        cli = self.mod.AnalysisClient(backend=AIBackend.CLI_AGENT)
        self.assertEqual(
            cli.service_address,
            f"{settings.cli_agent_grpc_host}:{settings.cli_agent_grpc_port}",
        )

    # ── _get_stub / pooling ───────────────────────────────────

    async def test_get_stub_creates_channel_and_stub(self):
        stub = await self.client._get_stub()
        self.assertIs(stub, self.stub)
        self.mod.grpc.aio.insecure_channel.assert_called_once()
        args, kwargs = self.mod.grpc.aio.insecure_channel.call_args
        self.assertEqual(args[0], self.client.service_address)
        self.assertIn(("grpc.max_receive_message_length", 50 * 1024 * 1024), kwargs["options"])
        self.assertIs(self.client._channel, self.channel)

    async def test_get_stub_reused_across_clients(self):
        client_two = self.mod.AnalysisClient()
        stub_one = await self.client._get_stub()
        stub_two = await client_two._get_stub()
        self.assertIs(stub_one, stub_two)
        self.mod.grpc.aio.insecure_channel.assert_called_once()

    async def test_get_stub_recreates_after_channel_shutdown(self):
        await self.client._get_stub()
        self.channel.get_state.return_value = grpc.ChannelConnectivity.SHUTDOWN
        stub = await self.client._get_stub()
        self.assertIs(stub, self.stub)
        self.assertEqual(self.mod.grpc.aio.insecure_channel.call_count, 2)

    async def test_get_stub_import_error_raises_runtime_error(self):
        with patch(
            "app.grpc_clients.analysis_pb2_grpc.AnalysisServiceStub",
            side_effect=ImportError("stubs missing"),
        ):
            client = self.mod.AnalysisClient()
            with self.assertRaises(RuntimeError) as ctx:
                await client._get_stub()
        self.assertIn("gRPC stubs not generated", str(ctx.exception))

    # ── analyze_market ────────────────────────────────────────

    async def test_analyze_market_success(self):
        result = await self.client.analyze_market(
            "Title", "Desc", 0.6, 0.4, 1234.5, "2026-12-31", include_research=False
        )
        self.assertEqual(result["analysis"], "analysis text")
        self.assertEqual(result["research_context"], "research-context")
        self.assertEqual(result["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["metadata"], {"key": "value"})
        self.stub.AnalyzeMarket.assert_awaited_once()
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.market_title, "Title")
        self.assertEqual(request.market_description, "Desc")
        self.assertEqual(request.yes_price, 0.6)
        self.assertEqual(request.no_price, 0.4)
        self.assertEqual(request.volume_24h, 1234.5)
        self.assertEqual(request.end_date, "2026-12-31")
        self.assertFalse(request.include_research)
        timeout = self.stub.AnalyzeMarket.await_args.kwargs["timeout"]
        self.assertEqual(timeout, get_settings().grpc_timeout)

    async def test_analyze_market_with_llm_config(self):
        await self.client.analyze_market(
            "T",
            "D",
            0.5,
            0.5,
            0,
            "2026-12-31",
            llm_config={"provider": 1, "model": "gpt-4", "temperature": 0.3, "max_tokens": 100},
        )
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.llm_config.provider, 1)
        self.assertEqual(request.llm_config.model, "gpt-4")
        self.assertAlmostEqual(request.llm_config.temperature, 0.3)
        self.assertEqual(request.llm_config.max_tokens, 100)

    async def test_analyze_market_grpc_error(self):
        self.stub.AnalyzeMarket = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError) as ctx:
            await self.client.analyze_market("T", "D", 0.5, 0.5, 0, "2026-12-31")
        self.assertIn("gRPC error: StatusCode.UNAVAILABLE: backend down", str(ctx.exception))

    # ── quick_analysis ────────────────────────────────────────

    async def test_quick_analysis_success(self):
        result = await self.client.quick_analysis("Will it?", 0.65)
        self.assertEqual(result["analysis"], "analysis text")
        request = self.stub.QuickAnalysis.await_args.args[0]
        self.assertEqual(request.question, "Will it?")
        self.assertEqual(request.current_price, 0.65)

    async def test_quick_analysis_with_llm_config(self):
        await self.client.quick_analysis("Q", 0.5, llm_config={"model": "m"})
        request = self.stub.QuickAnalysis.await_args.args[0]
        self.assertEqual(request.llm_config.model, "m")

    async def test_quick_analysis_grpc_error(self):
        self.stub.QuickAnalysis = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.quick_analysis("Q", 0.5)

    # ── scan_markets ──────────────────────────────────────────

    async def test_scan_markets_success(self):
        markets = [
            {
                "title": "M1",
                "description": "D1",
                "yes_price": 0.6,
                "no_price": 0.4,
                "volume_24h": 100.0,
                "end_date": "2026-12-31",
                "market_id": "c1",
            },
            # minimal market: missing keys and None prices
            {"title": "M2", "yes_price": None, "no_price": None},
        ]
        result = await self.client.scan_markets(markets)
        self.assertEqual(result["analysis"], "analysis text")
        request = self.stub.ScanMarkets.await_args.args[0]
        self.assertEqual(len(request.markets), 2)
        self.assertEqual(request.markets[0].title, "M1")
        self.assertEqual(request.markets[0].yes_price, 0.6)
        self.assertEqual(request.markets[0].market_id, "c1")
        self.assertEqual(request.markets[1].title, "M2")
        self.assertEqual(request.markets[1].yes_price, 0.0)
        self.assertEqual(request.markets[1].no_price, 0.0)
        self.assertEqual(request.markets[1].volume_24h, 0.0)

    async def test_scan_markets_empty_list(self):
        await self.client.scan_markets([])
        request = self.stub.ScanMarkets.await_args.args[0]
        self.assertEqual(len(request.markets), 0)

    async def test_scan_markets_with_llm_config(self):
        await self.client.scan_markets([], llm_config={"provider": 3})
        request = self.stub.ScanMarkets.await_args.args[0]
        self.assertEqual(request.llm_config.provider, 3)

    async def test_scan_markets_grpc_error(self):
        self.stub.ScanMarkets = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.scan_markets([{"title": "M"}])

    # ── assess_risk ───────────────────────────────────────────

    async def test_assess_risk_success(self):
        result = await self.client.assess_risk("Title", 100.0, 0.5, 7, "corr info")
        self.assertEqual(result["analysis"], "analysis text")
        request = self.stub.AssessRisk.await_args.args[0]
        self.assertEqual(request.market_title, "Title")
        self.assertEqual(request.position_size, 100.0)
        self.assertEqual(request.entry_price, 0.5)
        self.assertEqual(request.days_to_expiry, 7)
        self.assertEqual(request.correlation_info, "corr info")

    async def test_assess_risk_defaults_and_llm_config(self):
        await self.client.assess_risk("T", 1.0, 0.5, 1, llm_config={"model": "m"})
        request = self.stub.AssessRisk.await_args.args[0]
        self.assertEqual(request.correlation_info, "")
        self.assertEqual(request.llm_config.model, "m")

    async def test_assess_risk_grpc_error(self):
        self.stub.AssessRisk = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.assess_risk("T", 1.0, 0.5, 1)

    # ── generate_trade_plan ───────────────────────────────────

    async def test_generate_trade_plan_success(self):
        order_book = {"bids": [[0.5, 10]], "asks": [[0.6, 5]]}
        result = await self.client.generate_trade_plan("buy_yes", "Title", 50.0, 0.55, order_book)
        self.assertEqual(result["analysis"], "analysis text")
        request = self.stub.GenerateTradePlan.await_args.args[0]
        self.assertEqual(request.action, "buy_yes")
        self.assertEqual(request.market_title, "Title")
        self.assertEqual(request.target_size, 50.0)
        self.assertEqual(request.current_price, 0.55)
        self.assertEqual(request.order_book_json, json.dumps(order_book))

    async def test_generate_trade_plan_without_order_book(self):
        await self.client.generate_trade_plan("sell_no", "T", 10.0, 0.4)
        request = self.stub.GenerateTradePlan.await_args.args[0]
        self.assertEqual(request.order_book_json, "{}")

    async def test_generate_trade_plan_grpc_error(self):
        self.stub.GenerateTradePlan = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.generate_trade_plan("buy_yes", "T", 10.0, 0.5)

    async def test_generate_trade_plan_with_llm_config(self):
        await self.client.generate_trade_plan("buy_yes", "T", 10.0, 0.5, llm_config={"provider": 2})
        request = self.stub.GenerateTradePlan.await_args.args[0]
        self.assertEqual(request.llm_config.provider, 2)

    # ── analyze_market_stream ─────────────────────────────────

    async def test_analyze_market_stream_yields_chunks(self):
        chunks = [
            c
            async for c in self.client.analyze_market_stream("T", "D", 0.6, 0.4, 10.0, "2026-12-31")
        ]
        self.assertEqual(chunks, ["chunk-one", "chunk-two"])
        request = self.stub.AnalyzeMarketStream.call_args.args[0]
        self.assertEqual(request.market_title, "T")
        self.assertEqual(
            self.stub.AnalyzeMarketStream.call_args.kwargs["timeout"],
            get_settings().grpc_timeout * 2,
        )

    async def test_analyze_market_stream_with_llm_config(self):
        async for _ in self.client.analyze_market_stream(
            "T", "D", 0.6, 0.4, 10.0, "2026-12-31", llm_config={"model": "m"}
        ):
            pass
        request = self.stub.AnalyzeMarketStream.call_args.args[0]
        self.assertEqual(request.llm_config.model, "m")

    async def test_analyze_market_stream_grpc_error(self):
        self.stub.AnalyzeMarketStream = MagicMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError) as ctx:
            async for _ in self.client.analyze_market_stream(
                "T", "D", 0.6, 0.4, 10.0, "2026-12-31"
            ):
                pass
        self.assertIn("gRPC streaming error", str(ctx.exception))

    # ── health_check ──────────────────────────────────────────

    async def test_health_check_healthy(self):
        self.assertTrue(await self.client.health_check())
        self.stub.HealthCheck.assert_awaited_once()
        self.assertEqual(self.stub.HealthCheck.await_args.kwargs["timeout"], 5)

    async def test_health_check_unhealthy(self):
        self.stub.HealthCheck = AsyncMock(return_value=analysis_pb2.HealthResponse(healthy=False))
        self.assertFalse(await self.client.health_check())

    async def test_health_check_exception_returns_false(self):
        self.stub.HealthCheck = AsyncMock(side_effect=RuntimeError("no channel"))
        self.assertFalse(await self.client.health_check())

    # ── analyze_trader ────────────────────────────────────────

    async def test_analyze_trader_success(self):
        result = await self.client.analyze_trader(
            "0xwallet", "Trader", 100.0, 0.6, 10, 5, "[{}]", "u1"
        )
        self.assertEqual(result["analysis"], "analysis text")
        request = self.stub.AnalyzeTrader.await_args.args[0]
        self.assertEqual(request.wallet_address, "0xwallet")
        self.assertEqual(request.display_name, "Trader")
        self.assertEqual(request.total_pnl, 100.0)
        self.assertEqual(request.win_rate, 0.6)
        self.assertEqual(request.trade_count, 10)
        self.assertEqual(request.markets_traded, 5)
        self.assertEqual(request.recent_trades_json, "[{}]")
        self.assertEqual(request.user_id, "u1")

    async def test_analyze_trader_defaults_and_llm_config(self):
        await self.client.analyze_trader("0xw", llm_config={"provider": 5})
        request = self.stub.AnalyzeTrader.await_args.args[0]
        self.assertEqual(request.display_name, "")
        self.assertEqual(request.total_pnl, 0.0)
        self.assertEqual(request.recent_trades_json, "[]")
        self.assertEqual(request.user_id, "")
        self.assertEqual(request.llm_config.provider, 5)

    async def test_analyze_trader_grpc_error(self):
        self.stub.AnalyzeTrader = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.analyze_trader("0xw")

    # ── analyze_trader_stream ─────────────────────────────────

    async def test_analyze_trader_stream_splits_sentences(self):
        self.stub.AnalyzeTrader = AsyncMock(
            return_value=_ok_response(analysis="First sentence. Second sentence!")
        )
        chunks = [c async for c in self.client.analyze_trader_stream("0xw")]
        self.assertEqual(chunks, ["First sentence. ", "Second sentence! "])

    async def test_analyze_trader_stream_empty_analysis(self):
        self.stub.AnalyzeTrader = AsyncMock(return_value=_ok_response(analysis=""))
        chunks = [c async for c in self.client.analyze_trader_stream("0xw")]
        self.assertEqual(chunks, ["No analysis available for this trader."])

    async def test_analyze_trader_stream_forwards_llm_config(self):
        self.stub.AnalyzeTrader = AsyncMock(return_value=_ok_response(analysis="One."))
        async for _ in self.client.analyze_trader_stream("0xw", llm_config={"model": "m"}):
            pass
        request = self.stub.AnalyzeTrader.await_args.args[0]
        self.assertEqual(request.llm_config.model, "m")

    # ── evaluate_copy_trade ───────────────────────────────────

    async def test_evaluate_copy_trade_success_with_metadata(self):
        response = _ok_response(analysis="copy analysis")
        response.metadata["recommendation"] = "reduce_size"
        response.metadata["confidence"] = "72.5"
        response.metadata["risk_level"] = "high"
        self.stub.EvaluateCopyTrade = AsyncMock(return_value=response)

        result = await self.client.evaluate_copy_trade(
            "0xtrader", "stats", "mid", "Title", "SELL", 10.0, 0.6, "profile", "u1"
        )
        self.assertEqual(result["analysis"], "copy analysis")
        self.assertEqual(result["recommendation"], "reduce_size")
        self.assertEqual(result["confidence"], 72.5)
        self.assertEqual(result["risk_level"], "high")
        request = self.stub.EvaluateCopyTrade.await_args.args[0]
        self.assertEqual(request.trader_wallet, "0xtrader")
        self.assertEqual(request.trade_side, "SELL")
        self.assertEqual(request.user_risk_profile, "profile")
        self.assertEqual(request.user_id, "u1")

    async def test_evaluate_copy_trade_defaults(self):
        result = await self.client.evaluate_copy_trade("0xtrader", "stats")
        self.assertEqual(result["recommendation"], "copy")
        self.assertEqual(result["confidence"], 50.0)
        self.assertEqual(result["risk_level"], "medium")

    async def test_evaluate_copy_trade_grpc_error(self):
        self.stub.EvaluateCopyTrade = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.evaluate_copy_trade("0xtrader", "stats")

    async def test_evaluate_copy_trade_with_llm_config(self):
        await self.client.evaluate_copy_trade("0xtrader", "stats", llm_config={"provider": 3})
        request = self.stub.EvaluateCopyTrade.await_args.args[0]
        self.assertEqual(request.llm_config.provider, 3)

    # ── evaluate_inverse_position ─────────────────────────────

    async def test_evaluate_inverse_position_full_metadata(self):
        response = _ok_response(analysis="flip analysis")
        response.metadata.update(
            {
                "recommendation": "flip",
                "confidence": "0.75",
                "reasoning": "because",
                "alt_outcome": "NO",
                "alt_token_id": "tok-2",
                "web_summary": "web",
                "x_summary": "x",
                "key_risks": '["risk-a", "risk-b"]',
            }
        )
        self.stub.EvaluateInversePosition = AsyncMock(return_value=response)

        result = await self.client.evaluate_inverse_position(
            "cond-1",
            "Title",
            "YES",
            0.6,
            "NO",
            0.4,
            0.2,
            [{"id": "alt"}],
            include_research=False,
            user_id="u1",
        )
        self.assertEqual(result["recommendation"], "flip")
        self.assertEqual(result["confidence"], 0.75)
        self.assertEqual(result["reasoning"], "because")
        self.assertEqual(result["alt_outcome"], "NO")
        self.assertEqual(result["alt_token_id"], "tok-2")
        self.assertEqual(result["web_summary"], "web")
        self.assertEqual(result["x_summary"], "x")
        self.assertEqual(result["key_risks"], ["risk-a", "risk-b"])
        request = self.stub.EvaluateInversePosition.await_args.args[0]
        self.assertEqual(request.condition_id, "cond-1")
        self.assertEqual(request.held_outcome, "YES")
        self.assertEqual(request.held_pct, 0.6)
        self.assertEqual(request.delta_pct, 0.2)
        self.assertEqual(request.alternatives_json, json.dumps([{"id": "alt"}]))
        self.assertFalse(request.include_research)
        self.assertEqual(request.user_id, "u1")

    async def test_evaluate_inverse_position_defaults_and_edge_cases(self):
        # key_risks as a list, confidence as None (TypeError) and as junk
        # (ValueError), plus missing metadata entirely.
        response = MagicMock()
        response.success = True
        response.analysis = "text"
        response.research_context = "rc"
        response.timestamp = "ts"
        response.metadata = {
            "confidence": None,
            "key_risks": ["direct", "list"],
        }
        self.stub.EvaluateInversePosition = AsyncMock(return_value=response)
        result = await self.client.evaluate_inverse_position(
            "c", "T", "YES", 0.5, "NO", 0.5, 0.0, None
        )
        self.assertEqual(result["recommendation"], "hold")
        self.assertEqual(result["confidence"], 0.0)  # TypeError → 0.0
        self.assertEqual(result["key_risks"], ["direct", "list"])
        self.assertEqual(result["reasoning"], "text")  # falls back to analysis
        self.assertEqual(result["alt_outcome"], "")
        self.assertEqual(request_alternatives(self.stub), "[]")

        response.metadata = {"confidence": "junk", "key_risks": "{invalid"}
        result = await self.client.evaluate_inverse_position("c", "T", "Y", 0.5, "N", 0.5, 0.0, [])
        self.assertEqual(result["confidence"], 0.0)  # ValueError → 0.0
        self.assertEqual(result["key_risks"], [])  # JSONDecodeError → []

        response.metadata = {"key_risks": 42}
        result = await self.client.evaluate_inverse_position("c", "T", "Y", 0.5, "N", 0.5, 0.0, [])
        self.assertEqual(result["key_risks"], [])  # unknown type → []

    async def test_evaluate_inverse_position_grpc_error(self):
        self.stub.EvaluateInversePosition = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.evaluate_inverse_position("c", "T", "Y", 0.5, "N", 0.5, 0.0, [])

    async def test_evaluate_inverse_position_with_llm_config(self):
        await self.client.evaluate_inverse_position(
            "c", "T", "Y", 0.5, "N", 0.5, 0.0, [], llm_config={"provider": 4}
        )
        request = self.stub.EvaluateInversePosition.await_args.args[0]
        self.assertEqual(request.llm_config.provider, 4)

    # ── generate_news ─────────────────────────────────────────

    async def test_generate_news_success(self):
        result = await self.client.generate_news(
            "Will it happen?",
            market_description="Desc",
            condition_id="0xcond-1",
            yes_price=0.6,
            no_price=0.4,
            volume_24h=500.0,
            end_date="2026-12-31",
            trader_stats_json="{}",
            smart_money_json="{}",
        )
        self.assertEqual(result["headline"], "Headline")
        self.assertEqual(result["summary"], "Summary")
        self.assertEqual(result["body"], "Body text")
        self.assertEqual(result["sentiment"], "bullish")
        self.assertEqual(result["confidence"], 0.75)
        self.assertEqual(result["key_insights"], ["insight-1", "insight-2"])
        self.assertEqual(result["trader_behavior_summary"], "trader-behaviour")
        self.assertEqual(result["market_outlook"], "market-outlook")
        self.assertEqual(result["tags"], ["tag-a", "tag-b"])
        # question mirrors market_question
        self.assertEqual(result["question"], "Will it happen?")
        self.assertEqual(result["market_question"], "Will it happen?")
        self.assertEqual(result["condition_id"], "0xcond-1")
        self.assertEqual(result["generated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(result["metadata"], {"k": "v"})
        request = self.stub.GenerateNews.await_args.args[0]
        self.assertEqual(request.market_question, "Will it happen?")
        self.assertEqual(request.yes_price, 0.6)
        self.assertEqual(
            self.stub.GenerateNews.await_args.kwargs["timeout"],
            get_settings().grpc_timeout * 2,
        )

    async def test_generate_news_defaults_and_llm_config(self):
        await self.client.generate_news("Q", llm_config={"provider": 6})
        request = self.stub.GenerateNews.await_args.args[0]
        self.assertEqual(request.market_description, "")
        self.assertEqual(request.yes_price, 0.5)
        self.assertEqual(request.no_price, 0.5)
        self.assertEqual(request.volume_24h, 0)
        self.assertEqual(request.trader_stats_json, "{}")
        self.assertEqual(request.smart_money_json, "{}")
        self.assertEqual(request.llm_config.provider, 6)

    async def test_generate_news_service_failure(self):
        self.stub.GenerateNews = AsyncMock(
            return_value=analysis_pb2.GenerateNewsResponse(success=False, error="model overloaded")
        )
        with self.assertRaises(RuntimeError) as ctx:
            await self.client.generate_news("Q")
        self.assertIn("News generation error: model overloaded", str(ctx.exception))

    async def test_generate_news_grpc_error(self):
        self.stub.GenerateNews = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.generate_news("Q")

    def test_parse_news_response_static(self):
        parsed = self.mod.AnalysisClient._parse_news_response(_news_response())
        self.assertEqual(parsed["headline"], "Headline")
        self.assertEqual(parsed["question"], "Will it happen?")
        self.assertEqual(parsed["metadata"], {"k": "v"})

    # ── generate_news_batch ───────────────────────────────────

    async def test_generate_news_batch_success(self):
        markets = [
            {"question": "Q1", "description": "D1", "condition_id": "c1"},
            {"question": "Q2"},
        ]
        results = await self.client.generate_news_batch(markets)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["headline"], "Headline")
        request = self.stub.GenerateNewsBatch.await_args.args[0]
        self.assertEqual(len(request.markets), 2)
        self.assertEqual(request.markets[0].market_question, "Q1")
        self.assertEqual(request.markets[0].condition_id, "c1")
        self.assertEqual(request.markets[1].market_question, "Q2")
        self.assertEqual(request.markets[1].market_description, "")

    async def test_generate_news_batch_with_llm_config(self):
        # the per-request LLM config is forwarded on each market request
        await self.client.generate_news_batch([{"question": "Q"}], llm_config={"model": "m"})
        request = self.stub.GenerateNewsBatch.await_args.args[0]
        self.assertEqual(request.markets[0].llm_config.model, "m")

    async def test_generate_news_batch_service_failure(self):
        self.stub.GenerateNewsBatch = AsyncMock(
            return_value=analysis_pb2.GenerateNewsBatchResponse(success=False, error="batch failed")
        )
        with self.assertRaises(RuntimeError) as ctx:
            await self.client.generate_news_batch([{"question": "Q"}])
        self.assertIn("News batch generation error: batch failed", str(ctx.exception))

    async def test_generate_news_batch_grpc_error(self):
        self.stub.GenerateNewsBatch = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.generate_news_batch([{"question": "Q"}])

    # ── _parse_response / close ───────────────────────────────

    def test_parse_response_success(self):
        result = self.client._parse_response(_ok_response())
        self.assertEqual(result["analysis"], "analysis text")
        self.assertEqual(result["metadata"], {"key": "value"})

    def test_parse_response_without_metadata(self):
        result = self.client._parse_response(
            analysis_pb2.AnalysisResponse(success=True, analysis="a")
        )
        self.assertEqual(result["metadata"], {})

    def test_parse_response_failure(self):
        response = analysis_pb2.AnalysisResponse(success=False, error="boom")
        with self.assertRaises(RuntimeError) as ctx:
            self.client._parse_response(response)
        self.assertIn("Analysis service error: boom", str(ctx.exception))

    async def test_close_clears_channel_and_stub(self):
        self.client._channel = self.channel
        self.client._stub = self.stub
        await self.client.close()
        self.assertIsNone(self.client._channel)
        self.assertIsNone(self.client._stub)

    # ── scan_opportunity ──────────────────────────────────────

    async def test_scan_opportunity_success(self):
        raw = '```json\n{"ai_score": 77, "risk_level": "low", "pnl_potential": 12.5, '
        raw += '"credibility_score": 88, "smart_money_signal": "bullish", '
        raw += '"smart_money_summary": "whales buying", "recommendation": "buy", '
        raw += '"recommended_side": "YES", "reasoning": "strong", '
        raw += '"search_summary": "news", "key_risks": ["r1"]}\n```'
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis=raw))
        result = await self.client.scan_opportunity(
            "Title",
            "Desc",
            0.4,
            0.6,
            1000.0,
            "2026-12-31",
            pnl_potential=5.0,
            smart_money_context="ctx" * 100,
            liquidity=200.0,
            condition_id="cid-1",
        )
        self.assertEqual(result["condition_id"], "cid-1")
        self.assertEqual(result["market_title"], "Title")
        self.assertEqual(result["ai_score"], 77)
        self.assertEqual(result["risk_level"], "low")
        self.assertEqual(result["pnl_potential"], 12.5)
        self.assertEqual(result["credibility_score"], 88)
        self.assertEqual(result["smart_money_signal"], "bullish")
        self.assertEqual(result["smart_money_summary"], "whales buying")
        self.assertEqual(result["recommendation"], "buy")
        self.assertEqual(result["recommended_side"], "YES")
        self.assertEqual(result["reasoning"], "strong")
        self.assertEqual(result["search_summary"], "news")
        self.assertEqual(result["key_risks"], ["r1"])
        # description embeds the system prompt and the scoring template
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertIn("Superforecaster", request.market_description)
        self.assertIn("MARKET: Title", request.market_description)
        self.assertEqual(
            self.stub.AnalyzeMarket.await_args.kwargs["timeout"],
            get_settings().grpc_timeout * 2,
        )

    async def test_scan_opportunity_default_recommended_side(self):
        # yes_price >= no_price and no recommendation in the payload
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis='{"ai_score": 60}'))
        result = await self.client.scan_opportunity(
            "T", "D", 0.6, 0.4, 0.0, "2026-12-31", 1.0, "ctx"
        )
        self.assertEqual(result["recommended_side"], "NO")
        # defaults for missing keys
        self.assertEqual(result["risk_level"], "medium")
        self.assertEqual(result["pnl_potential"], 1.0)  # falls back to arg
        self.assertEqual(result["credibility_score"], 50)
        self.assertEqual(result["smart_money_signal"], "neutral")
        self.assertEqual(result["smart_money_summary"], "No data")
        self.assertEqual(result["recommendation"], "hold")
        self.assertEqual(result["key_risks"], [])

    async def test_scan_opportunity_yes_side_default(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{}"))
        result = await self.client.scan_opportunity(
            "T", "D", 0.3, 0.7, 0.0, "2026-12-31", 1.0, "ctx"
        )
        self.assertEqual(result["recommended_side"], "YES")

    async def test_scan_opportunity_without_template(self):
        with patch.object(self.mod, "_load_prompts", return_value={}):
            self.stub.AnalyzeMarket = AsyncMock(
                return_value=_ok_response(analysis='{"ai_score": 55}')
            )
            result = await self.client.scan_opportunity(
                "T", "", 0.5, 0.5, 0.0, "2026-12-31", 1.0, "ctx"
            )
        self.assertEqual(result["ai_score"], 55)
        request = self.stub.AnalyzeMarket.await_args.args[0]
        # empty description falls back to the title; fallback template used
        self.assertIn("T", request.market_description)
        self.assertIn("SMART MONEY DATA", request.market_description)

    async def test_scan_opportunity_invalid_json(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{not valid json}"))
        result = await self.client.scan_opportunity("T", "D", 0.5, 0.5, 0.0, "e", 1.0, "c")
        self.assertEqual(result["ai_score"], 50)
        self.assertEqual(result["reasoning"], "{not valid json}"[:300])

    async def test_scan_opportunity_no_json_in_raw(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="no braces here"))
        result = await self.client.scan_opportunity("T", "D", 0.5, 0.5, 0.0, "e", 1.0, "c")
        self.assertEqual(result["ai_score"], 50)
        self.assertEqual(result["reasoning"], "no braces here")

    async def test_scan_opportunity_rpc_exception(self):
        self.stub.AnalyzeMarket = AsyncMock(side_effect=RuntimeError("dead"))
        result = await self.client.scan_opportunity("T", "D", 0.5, 0.5, 0.0, "e", 1.0, "c")
        self.assertEqual(result["ai_score"], 50)
        self.assertEqual(result["reasoning"], "Analysis unavailable")

    async def test_scan_opportunity_response_not_success(self):
        self.stub.AnalyzeMarket = AsyncMock(
            return_value=analysis_pb2.AnalysisResponse(success=False, error="nope")
        )
        result = await self.client.scan_opportunity("T", "D", 0.5, 0.5, 0.0, "e", 1.0, "c")
        self.assertEqual(result["ai_score"], 50)

    async def test_scan_opportunity_with_llm_config(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{}"))
        await self.client.scan_opportunity(
            "T", "D", 0.5, 0.5, 0.0, "e", 1.0, "c", llm_config={"model": "m"}
        )
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.llm_config.model, "m")

    # ── scan_event_opportunity ────────────────────────────────

    async def test_scan_event_opportunity_success(self):
        raw = json.dumps(
            {
                "ai_score": 82,
                "risk_level": "low",
                "pnl_potential": 0.75,
                "credibility_score": 90,
                "smart_money_signal": "bullish",
                "smart_money_summary": "sum",
                "recommendation": "buy",
                "recommended_side": "YES",
                "recommended_option": "Option A",
                "reasoning": "r",
                "search_summary": "s",
                "key_risks": ["k"],
            }
        )
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis=raw))
        sub_markets = [
            {
                "label": "Option A",
                "yes_price": 0.6,
                "no_price": 0.4,
                "volume_24h": 100,
                "liquidity": 50,
            },
            {"question": "Option B", "yes_price": 0.3, "no_price": 0.7},
        ]
        result = await self.client.scan_event_opportunity(
            "Event",
            sub_markets,
            event_volume="1000",
            event_liquidity="500",
            smart_money_context="ctx",
        )
        self.assertEqual(result["event_title"], "Event")
        self.assertEqual(result["ai_score"], 82)
        self.assertEqual(result["recommended_option"], "Option A")
        self.assertEqual(result["recommended_side"], "YES")
        self.assertEqual(result["key_risks"], ["k"])
        request = self.stub.AnalyzeMarket.await_args.args[0]
        # sub-market table is embedded
        self.assertIn("Option A", request.market_description)
        self.assertIn("Option B", request.market_description)
        # representative prices come from the first sub-market
        self.assertEqual(request.yes_price, 0.6)
        self.assertEqual(request.no_price, 0.4)
        self.assertEqual(request.volume_24h, 1000.0)

    async def test_scan_event_opportunity_empty_sub_markets(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis='{"ai_score": 44}'))
        result = await self.client.scan_event_opportunity("Event", [])
        self.assertEqual(result["ai_score"], 44)
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.yes_price, 0.5)
        self.assertEqual(request.no_price, 0.5)
        self.assertEqual(request.volume_24h, 0)

    async def test_scan_event_opportunity_without_template(self):
        with patch.object(self.mod, "_load_prompts", return_value={}):
            self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{}"))
            result = await self.client.scan_event_opportunity(
                "Event", [{"label": "A", "yes_price": 0.5, "no_price": 0.5}]
            )
        self.assertEqual(result["ai_score"], 50)
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertIn("Respond with JSON", request.market_description)

    async def test_scan_event_opportunity_invalid_json(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{bad json}"))
        result = await self.client.scan_event_opportunity(
            "Event", [{"label": "A", "yes_price": 0.5, "no_price": 0.5}]
        )
        self.assertEqual(result["ai_score"], 50)
        self.assertEqual(result["recommended_option"], "")

    async def test_scan_event_opportunity_rpc_exception(self):
        self.stub.AnalyzeMarket = AsyncMock(side_effect=Exception("boom"))
        result = await self.client.scan_event_opportunity(
            "Event", [{"label": "A", "yes_price": 0.5, "no_price": 0.5}]
        )
        self.assertEqual(result["ai_score"], 50)
        self.assertEqual(result["reasoning"], "Analysis unavailable")

    async def test_scan_event_opportunity_with_llm_config(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{}"))
        await self.client.scan_event_opportunity(
            "Event",
            [{"label": "A", "yes_price": 0.5, "no_price": 0.5}],
            llm_config={"provider": 4},
        )
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.llm_config.provider, 4)

    # ── scan_easy_trades ──────────────────────────────────────

    async def test_scan_easy_trades_success(self):
        raw = json.dumps(
            {
                "easy_trades": [
                    {
                        "condition_id": "c1",
                        "market_title": "M1",
                        "ease_score": 85,
                        "risk_level": "low",
                        "edge_estimate": 12.5,
                        "confidence": 0.9,
                        "recommended_side": "YES",
                        "recommended_option": "opt",
                        "reasoning": "obvious",
                        "category": "misc",
                        "expected_probability": 0.7,
                    },
                    {"ease_score": 65},
                    {"ease_score": 45},
                    {"ease_score": 30},
                    "not-a-dict",
                ],
                "scan_summary": "scanned",
            }
        )
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis=raw))
        markets = [
            {
                "question": "Q1",
                "condition_id": "c1",
                "outcomePrices": ["0.6", "0.4"],
                "volume24hr": 100,
                "liquidity": 50,
                "endDate": "2026-12-31T00:00:00Z",
            },
            {
                "question": "Q2",
                "conditionId": "c2",
                "outcomePrices": ["0.3"],
                "id": "ignored",
            },
            {"question": "Q3", "yes_price": 0.7},
        ]
        results = await self.client.scan_easy_trades(markets, max_results=10)
        self.assertEqual(len(results), 4)  # non-dict entry skipped
        # sorted by ease_score descending
        self.assertEqual([r["ease_score"] for r in results], [85, 65, 45, 30])

        first = results[0]
        self.assertEqual(first["condition_id"], "c1")
        self.assertEqual(first["market_title"], "M1")
        self.assertEqual(first["ai_score"], 85)
        self.assertEqual(first["risk_level"], "low")
        self.assertEqual(first["pnl_potential"], 12.5)
        self.assertEqual(first["credibility_score"], 90)
        self.assertEqual(first["recommendation"], "strong_buy")
        self.assertEqual(first["recommended_side"], "YES")
        self.assertEqual(first["recommended_option"], "opt")
        self.assertEqual(first["reasoning"], "obvious")
        self.assertEqual(first["search_summary"], "scanned")
        self.assertEqual(first["expected_probability"], 0.7)
        self.assertEqual(first["edge_estimate"], 12.5)
        self.assertEqual(first["confidence"], 0.9)
        self.assertEqual(first["category"], "misc")
        self.assertEqual(first["smart_money_signal"], "neutral")
        self.assertEqual(first["key_risks"], [])

        self.assertEqual(results[1]["recommendation"], "buy")
        self.assertEqual(results[2]["recommendation"], "hold")
        self.assertEqual(results[3]["recommendation"], "avoid")
        # defaults applied to the sparse trade dict
        self.assertEqual(results[1]["condition_id"], "")
        self.assertEqual(results[1]["market_title"], "")
        self.assertEqual(results[1]["risk_level"], "medium")
        self.assertEqual(results[1]["pnl_potential"], 0.0)
        self.assertEqual(results[1]["credibility_score"], 50)
        self.assertEqual(results[1]["recommended_side"], "YES")
        self.assertEqual(results[1]["expected_probability"], 0.5)
        self.assertEqual(results[1]["category"], "other")

        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertIn("Q1", request.market_description)
        self.assertIn("c2...", request.market_description)
        self.assertEqual(request.market_title, "Easy Trade Scan — 3 newest markets")
        # representative prices from the first market
        self.assertEqual(request.yes_price, 0.6)
        self.assertEqual(request.no_price, 0.4)
        self.assertEqual(
            self.stub.AnalyzeMarket.await_args.kwargs["timeout"],
            get_settings().grpc_timeout * 3,
        )

    async def test_scan_easy_trades_max_results_truncation(self):
        raw = json.dumps(
            {
                "easy_trades": [
                    {"ease_score": 90},
                    {"ease_score": 80},
                    {"ease_score": 70},
                ]
            }
        )
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis=raw))
        results = await self.client.scan_easy_trades(
            [{"question": "Q", "outcomePrices": ["0.5", "0.5"]}], max_results=2
        )
        self.assertEqual(len(results), 2)
        self.assertEqual([r["ease_score"] for r in results], [90, 80])

    async def test_scan_easy_trades_invalid_json(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{bad json}"))
        results = await self.client.scan_easy_trades([{"question": "Q"}])
        self.assertEqual(results, [])

    async def test_scan_easy_trades_empty_markets(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{}"))
        results = await self.client.scan_easy_trades([])
        self.assertEqual(results, [])
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.market_title, "Easy Trade Scan — 0 newest markets")
        self.assertEqual(request.yes_price, 0.5)
        self.assertEqual(request.no_price, 0.5)

    async def test_scan_easy_trades_price_parse_errors(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{}"))
        markets = [
            {"question": "Q", "outcomePrices": ["abc"]},  # ValueError
            {"question": "Q2", "outcomePrices": ["0.6"]},  # single price
            {"question": "Q3"},  # no prices at all
        ]
        await self.client.scan_easy_trades(markets)
        request = self.stub.AnalyzeMarket.await_args.args[0]
        # first market's prices failed to parse → 0.5/0.5
        self.assertEqual(request.yes_price, 0.5)
        self.assertEqual(request.no_price, 0.5)
        self.assertIn("50.0¢", request.market_description)
        self.assertIn("60.0¢", request.market_description)

    async def test_scan_easy_trades_without_template(self):
        with patch.object(self.mod, "_load_prompts", return_value={}):
            self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{}"))
            await self.client.scan_easy_trades([{"question": "Q"}], max_results=3)
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertIn("Scan these markets for easy", request.market_description)

    async def test_scan_easy_trades_rpc_exception(self):
        self.stub.AnalyzeMarket = AsyncMock(side_effect=RuntimeError("dead"))
        results = await self.client.scan_easy_trades([{"question": "Q"}])
        self.assertEqual(results, [])

    async def test_scan_easy_trades_non_list_easy_trades(self):
        raw = json.dumps({"easy_trades": "not-a-list"})
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis=raw))
        results = await self.client.scan_easy_trades([{"question": "Q"}])
        self.assertEqual(results, [])

    # ── quick_group_analysis ──────────────────────────────────

    async def test_quick_group_analysis_success(self):
        raw = json.dumps(
            {
                "analysis": "Grouped analysis text",
                "recommended_option": "Option A",
                "recommended_side": "YES",
            }
        )
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis=raw))
        sub_markets = [
            {
                "label": "Option A",
                "question": "Q A",
                "yes_price": 0.4,
                "no_price": 0.6,
                "volume_24h": 100,
                "liquidity": 10,
            },
            {
                "label": "Option B",
                "question": "Q B",
                "yes_price": 0.7,
                "no_price": 0.3,
                "volume_24h": 200,
                "liquidity": 20,
            },
        ]
        result = await self.client.quick_group_analysis(
            "Event",
            sub_markets,
            event_volume=1000,
            event_liquidity=500,
            event_slug="slug",
        )
        self.assertEqual(result["analysis"], "Grouped analysis text")
        self.assertEqual(result["recommended_option"], "Option A")
        self.assertEqual(result["recommended_side"], "YES")
        request = self.stub.AnalyzeMarket.await_args.args[0]
        # representative = sub-market with the highest yes_price (Option B)
        self.assertEqual(request.yes_price, 0.7)
        self.assertEqual(request.no_price, 0.3)
        self.assertEqual(request.volume_24h, 1000.0)
        self.assertFalse(request.include_research)
        self.assertIn("Option A", request.market_description)
        self.assertIn("slug", request.market_description)
        self.assertEqual(
            self.stub.AnalyzeMarket.await_args.kwargs["timeout"],
            get_settings().grpc_timeout * 2,
        )

    async def test_quick_group_analysis_with_llm_config(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis='{"analysis": "a"}'))
        await self.client.quick_group_analysis(
            "Event",
            [{"label": "A", "yes_price": 0.5, "no_price": 0.5}],
            llm_config={"model": "m"},
        )
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.llm_config.model, "m")

    async def test_quick_group_analysis_empty_sub_markets(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis='{"analysis": "a"}'))
        result = await self.client.quick_group_analysis("Event", [])
        self.assertEqual(result["analysis"], "a")
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertEqual(request.yes_price, 0.5)
        self.assertEqual(request.no_price, 0.5)

    async def test_quick_group_analysis_without_template(self):
        with patch.object(self.mod, "_load_prompts", return_value={}):
            self.stub.AnalyzeMarket = AsyncMock(
                return_value=_ok_response(analysis='{"analysis": "a"}')
            )
            await self.client.quick_group_analysis(
                "Event", [{"label": "A", "yes_price": 0.5, "no_price": 0.5}]
            )
        request = self.stub.AnalyzeMarket.await_args.args[0]
        self.assertIn("Respond with JSON only", request.market_description)

    async def test_quick_group_analysis_representative_fallback(self):
        # 21st sub-market has an unparseable yes_price: the table loop only
        # reads the first 20, but max() iterates all → exception → fallback
        # to sub_markets[0].
        sub_markets = [
            {"label": f"O{i}", "yes_price": 0.1 * (i + 1), "no_price": 0.5} for i in range(20)
        ]
        sub_markets.append({"label": "Bad", "yes_price": "abc", "no_price": 0.5})
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis='{"analysis": "a"}'))
        result = await self.client.quick_group_analysis("Event", sub_markets)
        self.assertEqual(result["analysis"], "a")
        request = self.stub.AnalyzeMarket.await_args.args[0]
        # representative fell back to sub_markets[0] (yes_price 0.1)
        self.assertAlmostEqual(request.yes_price, 0.1)

    async def test_quick_group_analysis_raw_fallback(self):
        # parsed JSON has no "analysis" key → fall back to the raw text
        self.stub.AnalyzeMarket = AsyncMock(
            return_value=_ok_response(analysis='{"recommended_option": "X"}')
        )
        result = await self.client.quick_group_analysis(
            "Event", [{"label": "A", "yes_price": 0.5, "no_price": 0.5}]
        )
        self.assertEqual(result["analysis"], '{"recommended_option": "X"}')
        self.assertEqual(result["recommended_option"], "X")
        self.assertEqual(result["recommended_side"], "")

    async def test_quick_group_analysis_invalid_json(self):
        self.stub.AnalyzeMarket = AsyncMock(return_value=_ok_response(analysis="{bad json}"))
        result = await self.client.quick_group_analysis(
            "Event", [{"label": "A", "yes_price": 0.5, "no_price": 0.5}]
        )
        self.assertEqual(result["analysis"], "{bad json}")

    async def test_quick_group_analysis_rpc_exception(self):
        self.stub.AnalyzeMarket = AsyncMock(side_effect=Exception("dead"))
        result = await self.client.quick_group_analysis(
            "Event", [{"label": "A", "yes_price": 0.5, "no_price": 0.5}]
        )
        self.assertEqual(result["analysis"], "Quick analysis unavailable")
        self.assertEqual(result["recommended_option"], "")
        self.assertEqual(result["recommended_side"], "")

    # ── analyze_sentiment ─────────────────────────────────────

    async def test_analyze_sentiment_success_json(self):
        self.stub.AnalyzeSentiment = AsyncMock(
            return_value=_ok_response(analysis='{"sentiment_score": 0.7, "label": "bullish"}')
        )
        result = await self.client.analyze_sentiment("Will it happen?")
        self.assertEqual(result["sentiment_score"], 0.7)
        self.assertEqual(result["label"], "bullish")
        self.assertEqual(result["metadata"], {"key": "value"})
        request = self.stub.AnalyzeSentiment.await_args.args[0]
        self.assertEqual(request.query, "Will it happen?")
        self.assertTrue(request.include_news)
        self.assertTrue(request.include_social)
        self.assertEqual(
            self.stub.AnalyzeSentiment.await_args.kwargs["timeout"],
            get_settings().grpc_timeout * 2,
        )

    async def test_analyze_sentiment_non_json_analysis(self):
        self.stub.AnalyzeSentiment = AsyncMock(
            return_value=_ok_response(analysis="plain text summary")
        )
        result = await self.client.analyze_sentiment("Q", include_news=False, include_social=False)
        self.assertEqual(result["summary"], "plain text summary")
        self.assertEqual(result["metadata"], {"key": "value"})
        request = self.stub.AnalyzeSentiment.await_args.args[0]
        self.assertFalse(request.include_news)
        self.assertFalse(request.include_social)

    async def test_analyze_sentiment_with_llm_config(self):
        self.stub.AnalyzeSentiment = AsyncMock(return_value=_ok_response(analysis="{}"))
        await self.client.analyze_sentiment("Q", llm_config={"provider": 2})
        request = self.stub.AnalyzeSentiment.await_args.args[0]
        self.assertEqual(request.llm_config.provider, 2)

    async def test_analyze_sentiment_service_failure(self):
        self.stub.AnalyzeSentiment = AsyncMock(
            return_value=analysis_pb2.AnalysisResponse(success=False, error="sentiment model down")
        )
        with self.assertRaises(RuntimeError) as ctx:
            await self.client.analyze_sentiment("Q")
        self.assertIn("sentiment model down", str(ctx.exception))

    async def test_analyze_sentiment_grpc_error(self):
        self.stub.AnalyzeSentiment = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError) as ctx:
            await self.client.analyze_sentiment("Q")
        self.assertIn("gRPC error", str(ctx.exception))

    # ── discover_best_trade ───────────────────────────────────

    async def test_discover_best_trade_success(self):
        self.stub.DiscoverBestTrade = AsyncMock(
            return_value=_ok_response(
                analysis='{"best_trade": {"condition_id": "c1"}, "all_candidates": []}'
            )
        )
        markets = [
            {
                "question": "Q1",
                "description": "D" * 600,  # truncated to 500 chars
                "outcomePrices": ["0.6", "0.4"],
                "volume24hr": 100,
                "endDate": "2026-12-31",
                "conditionId": "cid-1",
            },
            {
                "title": "T2",
                "yes_price": 0.7,
                "volume_24h": 5,
                "end_date": "2026-11-30",
                "market_id": "mid-2",
            },
            {"question": "Q3", "outcomePrices": ["bad"]},  # parse error
            {"question": "Q4", "outcomePrices": ["0.6"]},  # single price
        ]
        result = await self.client.discover_best_trade(markets, budget=250.0, risk_tolerance="high")
        self.assertEqual(result["best_trade"], {"condition_id": "c1"})
        self.assertEqual(result["all_candidates"], [])
        self.assertEqual(result["metadata"], {"key": "value"})
        request = self.stub.DiscoverBestTrade.await_args.args[0]
        self.assertEqual(request.budget, 250.0)
        self.assertEqual(request.risk_tolerance, "high")
        self.assertEqual(len(request.markets), 4)
        m0 = request.markets[0]
        self.assertEqual(m0.title, "Q1")
        self.assertEqual(len(m0.description), 500)
        self.assertEqual(m0.yes_price, 0.6)
        self.assertEqual(m0.no_price, 0.4)
        self.assertEqual(m0.volume_24h, 100.0)
        self.assertEqual(m0.end_date, "2026-12-31")
        self.assertEqual(m0.market_id, "cid-1")
        m1 = request.markets[1]
        self.assertEqual(m1.title, "T2")
        self.assertEqual(m1.yes_price, 0.7)
        self.assertAlmostEqual(m1.no_price, 0.3)
        self.assertEqual(m1.market_id, "mid-2")
        m2 = request.markets[2]
        self.assertEqual(m2.yes_price, 0.5)
        self.assertEqual(m2.no_price, 0.5)
        m3 = request.markets[3]
        self.assertEqual(m3.yes_price, 0.6)
        self.assertEqual(m3.no_price, 0.4)
        self.assertEqual(
            self.stub.DiscoverBestTrade.await_args.kwargs["timeout"],
            get_settings().grpc_timeout * 5,
        )

    async def test_discover_best_trade_with_llm_config(self):
        self.stub.DiscoverBestTrade = AsyncMock(return_value=_ok_response(analysis="{}"))
        await self.client.discover_best_trade([], llm_config={"model": "m"})
        request = self.stub.DiscoverBestTrade.await_args.args[0]
        self.assertEqual(request.llm_config.model, "m")

    async def test_discover_best_trade_non_json_analysis(self):
        self.stub.DiscoverBestTrade = AsyncMock(return_value=_ok_response(analysis="not json"))
        result = await self.client.discover_best_trade([])
        self.assertEqual(result["analysis"], "not json")
        self.assertEqual(result["metadata"], {"key": "value"})

    async def test_discover_best_trade_service_failure(self):
        self.stub.DiscoverBestTrade = AsyncMock(
            return_value=analysis_pb2.AnalysisResponse(success=False, error="discovery failed")
        )
        with self.assertRaises(RuntimeError) as ctx:
            await self.client.discover_best_trade([])
        self.assertIn("discovery failed", str(ctx.exception))

    async def test_discover_best_trade_grpc_error(self):
        self.stub.DiscoverBestTrade = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.discover_best_trade([])

    # ── index_markets_rag ─────────────────────────────────────

    async def test_index_markets_rag_success(self):
        self.stub.IndexMarketsRAG = AsyncMock(
            return_value=_ok_response(analysis="indexed 2 markets")
        )
        markets = [{"question": "Q1"}, {"question": "Q2"}]
        result = await self.client.index_markets_rag(markets, force_reindex=True)
        self.assertEqual(result["success"], True)
        self.assertEqual(result["message"], "indexed 2 markets")
        self.assertEqual(result["metadata"], {"key": "value"})
        request = self.stub.IndexMarketsRAG.await_args.args[0]
        self.assertEqual(request.markets_json, json.dumps(markets))
        self.assertTrue(request.force_reindex)

    async def test_index_markets_rag_defaults(self):
        self.stub.IndexMarketsRAG = AsyncMock(
            return_value=analysis_pb2.AnalysisResponse(success=False, analysis="nothing")
        )
        result = await self.client.index_markets_rag([])
        self.assertEqual(result["success"], False)
        self.assertEqual(result["message"], "nothing")
        self.assertEqual(result["metadata"], {})
        request = self.stub.IndexMarketsRAG.await_args.args[0]
        self.assertFalse(request.force_reindex)

    async def test_index_markets_rag_grpc_error(self):
        self.stub.IndexMarketsRAG = AsyncMock(side_effect=_FakeRpcError())
        with self.assertRaises(RuntimeError):
            await self.client.index_markets_rag([])

    # ── factory / shared channel lifecycle ────────────────────

    def test_get_analysis_client_factory(self):
        client = self.mod.get_analysis_client(AIBackend.CLI_AGENT)
        self.assertIsInstance(client, self.mod.AnalysisClient)
        self.assertEqual(client.backend, AIBackend.CLI_AGENT)
        default = self.mod.get_analysis_client()
        self.assertEqual(default.backend, get_settings().default_ai_backend)

    async def test_close_shared_channels_closes_and_clears(self):
        client = self.mod.AnalysisClient()
        await client._get_stub()
        self.assertEqual(len(self.mod._shared_channels), 1)
        await self.mod.close_shared_channels()
        self.channel.close.assert_awaited_once()
        self.assertEqual(self.mod._shared_channels, {})
        self.assertEqual(self.mod._shared_stubs, {})


def request_alternatives(stub):
    """Helper: return the alternatives_json of the last EvaluateInversePosition call."""
    return stub.EvaluateInversePosition.await_args.args[0].alternatives_json


if __name__ == "__main__":
    unittest.main()
