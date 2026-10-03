"""
Analysis Service gRPC Client
Provides a unified interface to communicate with AI backend services.
Supports per-request LLM provider selection via the gateway pattern.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import grpc

from app.config import AIBackend, get_settings

# These will be generated from proto
# Import after generation: from app.grpc_clients import analysis_pb2, analysis_pb2_grpc

logger = logging.getLogger(__name__)
settings = get_settings()
_shared_channels: dict[str, grpc.aio.Channel] = {}
_shared_stubs: dict[str, Any] = {}
_shared_channels_lock: asyncio.Lock | None = None


def _get_channels_lock() -> asyncio.Lock:
    """Lazy-initialise the lock inside the running event loop.

    Creating an asyncio.Lock() at module-import time (before an event loop is
    running) can bind to the wrong loop in older Python/asyncio versions.
    This helper defers creation until first use, guaranteeing the lock belongs
    to the active loop.
    """
    global _shared_channels_lock
    if _shared_channels_lock is None:
        _shared_channels_lock = asyncio.Lock()
    return _shared_channels_lock


@lru_cache(maxsize=1)
def _load_prompts() -> dict[str, Any]:
    """Load prompts from common runtime locations."""
    module_dir = Path(__file__).parent
    candidates = [
        module_dir / ".." / ".." / "prompts" / "prompts.json",
        module_dir / ".." / ".." / ".." / "prompts" / "prompts.json",
        Path("/app/prompts/prompts.json"),
    ]
    for path in candidates:
        try:
            with path.open() as f:
                return json.load(f)
        except Exception:
            logger.debug("Prompts file not readable at %s", path, exc_info=True)
            continue
    return {}


def _build_llm_config(llm_config: dict[str, Any] | None):
    """
    Build a proto LLMConfig message from a dictionary.

    Args:
        llm_config: Dictionary with provider, model, temperature, max_tokens

    Returns:
        LLMConfig proto message or None if llm_config is None/empty
    """
    if not llm_config:
        return None

    try:
        from app.grpc_clients import analysis_pb2

        config = analysis_pb2.LLMConfig()

        if "provider" in llm_config:
            config.provider = int(llm_config["provider"])
        if "model" in llm_config and llm_config["model"]:
            config.model = str(llm_config["model"])
        if "temperature" in llm_config and llm_config["temperature"]:
            config.temperature = float(llm_config["temperature"])
        if "max_tokens" in llm_config and llm_config["max_tokens"]:
            config.max_tokens = int(llm_config["max_tokens"])

        return config
    except (ImportError, AttributeError):
        # Proto not regenerated yet, or LLMConfig not available
        return None


class AnalysisClient:
    """
    gRPC client for AI analysis services.
    Supports both LLM Chain and CLI Agent backends.
    """

    def __init__(self, backend: AIBackend = None):
        """Initialize client for specified backend."""
        self.backend = backend or settings.default_ai_backend
        self._channel = None
        self._stub = None

    @property
    def service_address(self) -> str:
        """Get the gRPC service address based on backend."""
        if self.backend == AIBackend.LLM_CHAIN:
            return f"{settings.llm_chain_grpc_host}:{settings.llm_chain_grpc_port}"
        return f"{settings.cli_agent_grpc_host}:{settings.cli_agent_grpc_port}"

    async def _get_stub(self):
        """Get or create gRPC stub, reusing shared channels per address.

        Channels are cached in the module-level ``_shared_channels`` dict
        keyed by service address.  A connectivity check ensures stale /
        closed channels are replaced transparently.
        """
        addr = self.service_address

        # Fast path: already resolved for this instance
        if self._stub is not None:
            # Verify the shared channel is still alive
            if addr in _shared_channels:
                state = _shared_channels[addr].get_state(try_to_connect=False)
                if state != grpc.ChannelConnectivity.SHUTDOWN:
                    return self._stub
            # Channel gone or shut down – clear and re-create below
            self._stub = None
            self._channel = None

        try:
            from app.grpc_clients import analysis_pb2_grpc

            lock = _get_channels_lock()
            async with lock:
                # Double-check after acquiring the lock
                if addr in _shared_channels:
                    state = _shared_channels[addr].get_state(try_to_connect=False)
                    if state == grpc.ChannelConnectivity.SHUTDOWN:
                        # Remove stale entry
                        _shared_channels.pop(addr, None)
                        _shared_stubs.pop(addr, None)

                if addr not in _shared_channels:
                    channel = grpc.aio.insecure_channel(
                        addr,
                        options=[
                            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
                            ("grpc.keepalive_time_ms", 10000),
                            ("grpc.keepalive_timeout_ms", 5000),
                            ("grpc.http2.min_ping_interval_without_data_ms", 10000),
                        ],
                    )
                    _shared_channels[addr] = channel
                    _shared_stubs[addr] = analysis_pb2_grpc.AnalysisServiceStub(channel)

                self._channel = _shared_channels[addr]
                self._stub = _shared_stubs[addr]
        except ImportError:
            raise RuntimeError(
                "gRPC stubs not generated. Run: "
                "python -m grpc_tools.protoc -I./proto --python_out=./app/grpc_clients "
                "--grpc_python_out=./app/grpc_clients proto/analysis.proto"
            ) from None
        return self._stub

    async def analyze_market(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Full market analysis with research.

        Args:
            market_title: Title of the prediction market
            market_description: Full market description
            yes_price: Current YES token price (0-1)
            no_price: Current NO token price (0-1)
            volume_24h: 24-hour trading volume in USD
            end_date: Market resolution date (ISO format)
            include_research: Include web research
            llm_config: Optional LLM configuration override (provider, model, etc.)

        Returns:
            Analysis result dictionary
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "market_title": market_title,
            "market_description": market_description,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_24h": volume_24h,
            "end_date": end_date,
            "include_research": include_research,
        }

        # Add llm_config if provided and proto supports it
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        try:
            response = await stub.AnalyzeMarket(request, timeout=settings.grpc_timeout)
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def quick_analysis(
        self, question: str, current_price: float, llm_config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Quick 2-3 sentence market analysis.

        Args:
            question: Market question
            current_price: Current market price (0-1)
            llm_config: Optional LLM configuration override

        Returns:
            Quick analysis result
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "question": question,
            "current_price": current_price,
        }

        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.QuickAnalysisRequest(**request_kwargs)

        try:
            response = await stub.QuickAnalysis(request, timeout=settings.grpc_timeout)
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def scan_markets(
        self, markets: list, llm_config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Scan multiple markets to identify opportunities.

        Args:
            markets: List of market dictionaries
            llm_config: Optional LLM configuration override

        Returns:
            Scan results with ranked opportunities
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request = analysis_pb2.MarketScanRequest()
        for market in markets:
            request.markets.append(
                analysis_pb2.MarketInfo(
                    title=str(market.get("title", "")),
                    description=str(market.get("description", "")),
                    yes_price=float(market.get("yes_price", 0.0) or 0.0),
                    no_price=float(market.get("no_price", 0.0) or 0.0),
                    volume_24h=float(market.get("volume_24h", 0.0) or 0.0),
                    end_date=str(market.get("end_date", "")),
                    market_id=str(market.get("market_id", "")),
                )
            )

        # Add llm_config if provided
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request.llm_config.CopyFrom(proto_llm_config)

        try:
            response = await stub.ScanMarkets(request, timeout=settings.grpc_timeout)
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def assess_risk(
        self,
        market_title: str,
        position_size: float,
        entry_price: float,
        days_to_expiry: int,
        correlation_info: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Assess risk for a potential position.

        Args:
            market_title: Market title
            position_size: Position size in USD
            entry_price: Entry price (0-1)
            days_to_expiry: Days until market expiry
            correlation_info: Optional correlation information
            llm_config: Optional LLM configuration override

        Returns:
            Risk assessment result
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "market_title": market_title,
            "position_size": position_size,
            "entry_price": entry_price,
            "days_to_expiry": days_to_expiry,
            "correlation_info": correlation_info,
        }

        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.RiskAssessmentRequest(**request_kwargs)

        try:
            response = await stub.AssessRisk(request, timeout=settings.grpc_timeout)
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def generate_trade_plan(
        self,
        action: str,
        market_title: str,
        target_size: float,
        current_price: float,
        order_book: dict = None,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate execution plan for a trade.

        Args:
            action: Trade action (buy_yes, buy_no, sell_yes, sell_no)
            market_title: Market title
            target_size: Target size in USD
            current_price: Current price (0-1)
            order_book: Optional order book data
            llm_config: Optional per-request LLM configuration

        Returns:
            Trade execution plan
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "action": action,
            "market_title": market_title,
            "target_size": target_size,
            "current_price": current_price,
            "order_book_json": json.dumps(order_book or {}),
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.TradePlanRequest(**request_kwargs)

        try:
            response = await stub.GenerateTradePlan(request, timeout=settings.grpc_timeout)
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def analyze_market_stream(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True,
        llm_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """
        Streaming market analysis - yields chunks as they're generated.

        Yields:
            Analysis text chunks
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "market_title": market_title,
            "market_description": market_description,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_24h": volume_24h,
            "end_date": end_date,
            "include_research": include_research,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        try:
            async for chunk in stub.AnalyzeMarketStream(request, timeout=settings.grpc_timeout * 2):
                yield chunk.chunk
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC streaming error: {e.code()}: {e.details()}") from e

    async def health_check(self) -> bool:
        """Check if the backend service is healthy."""
        from app.grpc_clients import analysis_pb2

        try:
            stub = await self._get_stub()
            request = analysis_pb2.HealthRequest()
            response = await stub.HealthCheck(request, timeout=5)
            return response.healthy
        except Exception:
            return False

    async def analyze_trader(
        self,
        wallet_address: str,
        display_name: str = "",
        total_pnl: float = 0.0,
        win_rate: float = 0.0,
        trade_count: int = 0,
        markets_traded: int = 0,
        recent_trades_json: str = "[]",
        user_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Full trader profile analysis with internet research."""
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "wallet_address": wallet_address,
            "display_name": display_name,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "trade_count": trade_count,
            "markets_traded": markets_traded,
            "recent_trades_json": recent_trades_json,
            "user_id": user_id,
        }

        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.TraderAnalysisRequest(**request_kwargs)

        try:
            response = await stub.AnalyzeTrader(request, timeout=settings.grpc_timeout)
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def analyze_trader_stream(
        self,
        wallet_address: str,
        display_name: str = "",
        total_pnl: float = 0.0,
        win_rate: float = 0.0,
        trade_count: int = 0,
        markets_traded: int = 0,
        recent_trades_json: str = "[]",
        user_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """
        Streaming trader analysis - yields text chunks.
        Uses the unary AnalyzeTrader RPC and simulates streaming
        by yielding the response in sentence-sized chunks.
        """
        result = await self.analyze_trader(
            wallet_address=wallet_address,
            display_name=display_name,
            total_pnl=total_pnl,
            win_rate=win_rate,
            trade_count=trade_count,
            markets_traded=markets_traded,
            recent_trades_json=recent_trades_json,
            user_id=user_id,
            llm_config=llm_config,
        )

        analysis_text = result.get("analysis", "")
        if not analysis_text:
            yield "No analysis available for this trader."
            return

        # Stream in sentence-like chunks for a real-time feel
        import re as _re

        sentences = _re.split(r"(?<=[.!?\n])\s+", analysis_text)
        for sentence in sentences:
            if sentence.strip():
                yield sentence.strip() + " "
                await asyncio.sleep(0.05)

    async def evaluate_copy_trade(
        self,
        trader_wallet: str,
        trader_stats: str,
        market_id: str = "",
        market_title: str = "",
        trade_side: str = "BUY",
        trade_size: float = 0.0,
        current_price: float = 0.0,
        user_risk_profile: str = "max_position_daily_loss",
        user_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate whether to copy a specific trade."""
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "trader_wallet": trader_wallet,
            "trader_stats": trader_stats,
            "market_id": market_id,
            "market_title": market_title,
            "trade_side": trade_side,
            "trade_size": trade_size,
            "current_price": current_price,
            "user_risk_profile": user_risk_profile,
            "user_id": user_id,
        }

        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.CopyTradeEvaluationRequest(**request_kwargs)

        try:
            response = await stub.EvaluateCopyTrade(request, timeout=settings.grpc_timeout)
            result = self._parse_response(response)
            # Extract recommendation metadata
            meta = result.get("metadata", {})
            result["recommendation"] = meta.get("recommendation", "copy")
            result["confidence"] = float(meta.get("confidence", 50))
            result["risk_level"] = meta.get("risk_level", "medium")
            return result
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def evaluate_inverse_position(
        self,
        condition_id: str,
        market_title: str,
        held_outcome: str,
        held_pct: float,
        best_alt_outcome: str,
        best_alt_pct: float,
        delta_pct: float,
        alternatives_json: Any,
        include_research: bool = True,
        user_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate whether to reverse an open position."""
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "condition_id": condition_id,
            "market_title": market_title,
            "held_outcome": held_outcome,
            "held_pct": held_pct,
            "best_alt_outcome": best_alt_outcome,
            "best_alt_pct": best_alt_pct,
            "delta_pct": delta_pct,
            "alternatives_json": json.dumps(alternatives_json or []),
            "include_research": include_research,
            "user_id": user_id,
        }

        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.InversePositionEvaluationRequest(**request_kwargs)

        try:
            response = await stub.EvaluateInversePosition(
                request,
                timeout=settings.grpc_timeout,
            )
            result = self._parse_response(response)
            meta = result.get("metadata", {})
            result["recommendation"] = str(meta.get("recommendation", "hold"))
            try:
                result["confidence"] = float(meta.get("confidence", 0))
            except (TypeError, ValueError):
                result["confidence"] = 0.0
            result["reasoning"] = meta.get("reasoning", result.get("analysis", ""))
            result["alt_outcome"] = meta.get("alt_outcome", "")
            result["alt_token_id"] = meta.get("alt_token_id", "")
            result["web_summary"] = meta.get("web_summary", "")
            result["x_summary"] = meta.get("x_summary", "")
            key_risks_raw = meta.get("key_risks", "[]")
            if isinstance(key_risks_raw, str):
                try:
                    result["key_risks"] = json.loads(key_risks_raw)
                except json.JSONDecodeError:
                    result["key_risks"] = []
            elif isinstance(key_risks_raw, list):
                result["key_risks"] = key_risks_raw
            else:
                result["key_risks"] = []
            return result
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    async def generate_news(
        self,
        market_question: str,
        market_description: str = "",
        condition_id: str = "",
        yes_price: float = 0.5,
        no_price: float = 0.5,
        volume_24h: float = 0,
        end_date: str = "",
        trader_stats_json: str = "{}",
        smart_money_json: str = "{}",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate an AI-powered news article for a prediction market.

        Args:
            market_question: Market question text
            market_description: Full market description
            condition_id: Polymarket condition ID
            yes_price: Current YES price (0-1)
            no_price: Current NO price (0-1)
            volume_24h: 24h trading volume
            end_date: Market resolution date (ISO)
            trader_stats_json: JSON string of trader positioning data
            smart_money_json: JSON string of smart money analysis
            llm_config: Optional LLM configuration override

        Returns:
            News article dictionary with headline, summary, body, sentiment, etc.
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request_kwargs = {
            "market_question": market_question,
            "market_description": market_description,
            "condition_id": condition_id,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_24h": volume_24h,
            "end_date": end_date,
            "trader_stats_json": trader_stats_json,
            "smart_money_json": smart_money_json,
        }

        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.GenerateNewsRequest(**request_kwargs)

        try:
            response = await stub.GenerateNews(
                request,
                timeout=settings.grpc_timeout * 2,  # news generation may be slower
            )

            if not response.success:
                raise RuntimeError(f"News generation error: {response.error}")

            return self._parse_news_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    @staticmethod
    def _parse_news_response(response) -> dict[str, Any]:
        """Normalise a GenerateNewsResponse into the article dict shape.

        ``question`` mirrors ``market_question``: the frontend's
        ``NewsArticle`` type reads ``question``, while older
        consumers read ``market_question``.
        """
        return {
            "headline": response.headline,
            "summary": response.summary,
            "body": response.body,
            "sentiment": response.sentiment,
            "confidence": float(response.confidence),
            "key_insights": list(response.key_insights),
            "trader_behavior_summary": response.trader_behavior_summary,
            "market_outlook": response.market_outlook,
            "tags": list(response.tags),
            "question": response.market_question,
            "market_question": response.market_question,
            "condition_id": response.condition_id,
            "generated_at": response.generated_at,
            "metadata": dict(response.metadata) if response.metadata else {},
        }

    async def generate_news_batch(
        self,
        markets: list[dict[str, Any]],
        llm_config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Generate news articles for multiple markets in a single RPC.

        Args:
            markets: List of dicts with ``question`` and ``condition_id``
                keys (other GenerateNewsRequest fields are optional).
            llm_config: Optional LLM configuration override

        Returns:
            List of news article dictionaries (see ``generate_news``).
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        proto_llm_config = _build_llm_config(llm_config)
        requests = []
        for mkt in markets:
            request_kwargs = {
                "market_question": mkt.get("question", ""),
                "market_description": mkt.get("description", ""),
                "condition_id": mkt.get("condition_id", ""),
            }
            if proto_llm_config is not None:
                request_kwargs["llm_config"] = proto_llm_config
            requests.append(analysis_pb2.GenerateNewsRequest(**request_kwargs))

        request = analysis_pb2.GenerateNewsBatchRequest(markets=requests)

        try:
            response = await stub.GenerateNewsBatch(
                request,
                timeout=settings.grpc_timeout * 2,  # news generation may be slower
            )

            if not response.success:
                raise RuntimeError(f"News batch generation error: {response.error}")

            return [self._parse_news_response(article) for article in response.articles]
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    def _parse_response(self, response) -> dict[str, Any]:
        """Parse gRPC response to dictionary."""
        if not response.success:
            raise RuntimeError(f"Analysis service error: {response.error}")
        return {
            "analysis": response.analysis,
            "research_context": response.research_context,
            "metadata": dict(response.metadata) if response.metadata else {},
            "timestamp": response.timestamp,
        }

    async def close(self):
        """Close the gRPC channel."""
        self._channel = None
        self._stub = None

    async def scan_opportunity(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        pnl_potential: float,
        smart_money_context: str,
        liquidity: float = 0,
        condition_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Score a single market opportunity by leveraging the existing
        AnalyzeMarket RPC with an enriched opportunity-scoring prompt.
        Returns structured JSON scoring.
        """
        from app.grpc_clients import analysis_pb2

        # Load opportunity prompt templates.
        prompts = _load_prompts()

        system_prompt = (
            prompts.get("opportunity_analysis", {}).get("system_prompt", "")
            or "You are an expert prediction market analyst. Respond with ONLY valid JSON."
        )
        scoring_template = prompts.get("opportunity_analysis", {}).get("market_scoring", "")

        if scoring_template:
            enriched_desc = scoring_template.format(
                market_title=market_title,
                market_description=market_description or market_title,
                yes_price_cents=f"{yes_price * 100:.1f}",
                no_price_cents=f"{no_price * 100:.1f}",
                volume_24h=f"{volume_24h:,.0f}",
                liquidity=f"{liquidity:,.0f}",
                end_date=end_date,
                pnl_potential=f"{pnl_potential:.2f}",
                search_findings="(Use web search to validate credibility and find recent news)",
                smart_money_context=smart_money_context[:1500],
            )
        else:
            enriched_desc = (
                f"{market_description or market_title}\n\n"
                f"SMART MONEY DATA:\n{smart_money_context}\n\n"
                f"PNL Potential: {pnl_potential:.2f}\n"
                f"Respond with JSON: ai_score, risk_level, recommendation, reasoning"
            )

        # Prepend system instructions into the description
        full_description = f"{system_prompt}\n\n{enriched_desc}"

        stub = await self._get_stub()
        request_kwargs = {
            "market_title": market_title,
            "market_description": full_description,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_24h": volume_24h,
            "end_date": end_date,
            "include_research": True,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        try:
            response = await stub.AnalyzeMarket(request, timeout=settings.grpc_timeout * 2)
            raw = response.analysis if response.success else ""
        except Exception:
            raw = ""

        # Parse JSON from LLM response
        scored = {}
        if raw:
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if json_match:
                try:
                    scored = json.loads(json_match.group())
                except json.JSONDecodeError:
                    scored = {}

        return {
            "condition_id": condition_id,
            "market_title": market_title,
            "ai_score": int(scored.get("ai_score", 50)),
            "risk_level": scored.get("risk_level", "medium"),
            "pnl_potential": float(scored.get("pnl_potential", pnl_potential)),
            "credibility_score": int(scored.get("credibility_score", 50)),
            "smart_money_signal": scored.get("smart_money_signal", "neutral"),
            "smart_money_summary": scored.get("smart_money_summary", "No data"),
            "recommendation": scored.get("recommendation", "hold"),
            "recommended_side": scored.get(
                "recommended_side",
                "YES" if yes_price < no_price else "NO",
            ),
            "reasoning": scored.get("reasoning", raw[:300] if raw else "Analysis unavailable"),
            "search_summary": scored.get("search_summary", ""),
            "key_risks": scored.get("key_risks", []),
        }

    async def scan_event_opportunity(
        self,
        event_title: str,
        sub_markets: list,
        event_volume: str = "0",
        event_liquidity: str = "0",
        smart_money_context: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Score an entire event group (multiple related sub-markets) with
        ONE LLM call via the AnalyzeMarket RPC.  Returns structured JSON
        scoring that includes 'recommended_option'.
        """
        from app.grpc_clients import analysis_pb2

        # Load prompts
        prompts = _load_prompts()

        system_prompt = (
            prompts.get("opportunity_analysis", {}).get("system_prompt", "")
            or "You are an expert prediction market analyst. Respond with ONLY valid JSON."
        )

        # Build sub-markets table
        table_lines = [
            "| Option | YES Price | NO Price | 24h Volume | Liquidity |",
            "|--------|-----------|----------|------------|-----------|",
        ]
        for sm in sub_markets:
            label = sm.get("label", sm.get("question", "Unknown"))
            yes_p = sm.get("yes_price", 0)
            no_p = sm.get("no_price", 0)
            vol = sm.get("volume_24h", 0)
            liq = sm.get("liquidity", 0)
            table_lines.append(
                f"| {label} | {yes_p * 100:.1f}¢ | {no_p * 100:.1f}¢ | ${vol:,.0f} | ${liq:,.0f} |"
            )
        sub_markets_table = "\n".join(table_lines)

        scoring_template = prompts.get("opportunity_analysis", {}).get("event_scoring", "")
        if scoring_template:
            enriched_desc = scoring_template.format(
                event_title=event_title,
                sub_markets_table=sub_markets_table,
                event_volume=event_volume,
                event_liquidity=event_liquidity,
                search_findings="(Use web search to validate credibility and find recent news)",
                smart_money_context=smart_money_context[:1500],
            )
        else:
            enriched_desc = (
                f"Event: {event_title}\n\n{sub_markets_table}\n\n"
                f"SMART MONEY DATA:\n{smart_money_context}\n\n"
                f"Respond with JSON: ai_score, risk_level, recommendation, "
                f"recommended_option, reasoning"
            )

        full_description = f"{system_prompt}\n\n{enriched_desc}"

        # Use the first sub-market's prices as representative for the RPC
        first = sub_markets[0] if sub_markets else {}
        stub = await self._get_stub()
        request_kwargs = {
            "market_title": event_title,
            "market_description": full_description,
            "yes_price": first.get("yes_price", 0.5),
            "no_price": first.get("no_price", 0.5),
            "volume_24h": float(event_volume) if event_volume else 0,
            "end_date": "",
            "include_research": True,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        try:
            response = await stub.AnalyzeMarket(request, timeout=settings.grpc_timeout * 2)
            raw = response.analysis if response.success else ""
        except Exception:
            raw = ""

        scored = {}
        if raw:
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if json_match:
                try:
                    scored = json.loads(json_match.group())
                except json.JSONDecodeError:
                    scored = {}

        return {
            "event_title": event_title,
            "ai_score": int(scored.get("ai_score", 50)),
            "risk_level": scored.get("risk_level", "medium"),
            "pnl_potential": float(scored.get("pnl_potential", 0.5)),
            "credibility_score": int(scored.get("credibility_score", 50)),
            "smart_money_signal": scored.get("smart_money_signal", "neutral"),
            "smart_money_summary": scored.get("smart_money_summary", "No data"),
            "recommendation": scored.get("recommendation", "hold"),
            "recommended_side": scored.get("recommended_side", "YES"),
            "recommended_option": scored.get("recommended_option", ""),
            "reasoning": scored.get("reasoning", raw[:300] if raw else "Analysis unavailable"),
            "search_summary": scored.get("search_summary", ""),
            "key_risks": scored.get("key_risks", []),
        }

    async def scan_easy_trades(
        self,
        markets: list,
        max_results: int = 10,
    ) -> list:
        """
        Batch-scan a list of newest markets to identify the easiest/most
        obvious trades.  Sends all markets in ONE LLM call and returns a
        ranked list of easy-trade scores.

        Falls back to splitting into smaller batches if the market list is
        too large (> 20 markets per batch).
        """
        from app.grpc_clients import analysis_pb2

        prompts = _load_prompts()

        system_prompt = (
            prompts.get("easy_trade_analysis", {}).get("system_prompt", "")
            or "You are an expert prediction market analyst. Respond with ONLY valid JSON."
        )
        scoring_template = prompts.get("easy_trade_analysis", {}).get("batch_scoring", "")

        # Build markdown table of markets
        table_lines = [
            "| # | Question | Condition ID | YES Price | NO Price "
            "| 24h Volume | Liquidity | End Date |",
            "|---|----------|--------------|-----------|----------|"
            "------------|-----------|----------|",
        ]
        for i, m in enumerate(markets, 1):
            question = m.get("question", "Unknown")[:80]
            cid = m.get("condition_id") or m.get("conditionId") or m.get("id", "")
            prices = m.get("outcomePrices", [])
            try:
                yes_p = float(prices[0]) if prices else 0.5
                no_p = float(prices[1]) if len(prices) > 1 else 1.0 - yes_p
                vol = float(m.get("volume24hr", 0) or 0)
                liq = float(m.get("liquidity", 0) or 0)
            except (ValueError, TypeError):
                yes_p, no_p = 0.5, 0.5
                vol, liq = 0.0, 0.0
            end = m.get("endDate") or m.get("end_date_iso") or ""
            table_lines.append(
                f"| {i} | {question} | {cid[:16]}... | {yes_p * 100:.1f}¢ "
                f"| {no_p * 100:.1f}¢ | ${vol:,.0f} | ${liq:,.0f} | {end[:10]} |"
            )
        markets_table = "\n".join(table_lines)

        if scoring_template:
            enriched_desc = scoring_template.format(
                markets_table=markets_table,
                max_results=max_results,
            )
        else:
            enriched_desc = (
                f"Scan these markets for easy, obvious trades:\n\n{markets_table}\n\n"
                f"Return top {max_results} easiest trades as JSON with ease_score, "
                f"recommended_side, reasoning."
            )

        full_description = f"{system_prompt}\n\n{enriched_desc}"

        stub = await self._get_stub()
        first = markets[0] if markets else {}
        first_prices = first.get("outcomePrices", ["0.5", "0.5"])
        try:
            yes_p = float(first_prices[0])
            no_p = float(first_prices[1]) if len(first_prices) > 1 else 1.0 - yes_p
        except (ValueError, TypeError):
            yes_p, no_p = 0.5, 0.5

        request = analysis_pb2.MarketAnalysisRequest(
            market_title=f"Easy Trade Scan — {len(markets)} newest markets",
            market_description=full_description,
            yes_price=yes_p,
            no_price=no_p,
            volume_24h=0,
            end_date="",
            include_research=True,
        )

        try:
            response = await stub.AnalyzeMarket(request, timeout=settings.grpc_timeout * 3)
            raw = response.analysis if response.success else ""
        except Exception as e:
            logger.error("Easy trade scan gRPC failed: %s", e)
            raw = ""

        results = []
        if raw:
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                    easy_trades = parsed.get("easy_trades", [])
                    if isinstance(easy_trades, list):
                        for trade in easy_trades:
                            if not isinstance(trade, dict):
                                continue
                            results.append(
                                {
                                    "condition_id": trade.get("condition_id", ""),
                                    "market_title": trade.get("market_title", ""),
                                    "ai_score": int(trade.get("ease_score", 50)),
                                    "ease_score": int(trade.get("ease_score", 50)),
                                    "risk_level": trade.get("risk_level", "medium"),
                                    "pnl_potential": abs(float(trade.get("edge_estimate", 0))),
                                    "credibility_score": int(trade.get("confidence", 0.5) * 100),
                                    "smart_money_signal": "neutral",
                                    "smart_money_summary": "",
                                    "recommendation": (
                                        "strong_buy"
                                        if trade.get("ease_score", 0) >= 80
                                        else "buy"
                                        if trade.get("ease_score", 0) >= 60
                                        else "hold"
                                        if trade.get("ease_score", 0) >= 40
                                        else "avoid"
                                    ),
                                    "recommended_side": trade.get("recommended_side", "YES"),
                                    "recommended_option": trade.get("recommended_option", ""),
                                    "reasoning": trade.get("reasoning", ""),
                                    "search_summary": parsed.get("scan_summary", ""),
                                    "key_risks": [],
                                    "expected_probability": float(
                                        trade.get("expected_probability", 0.5)
                                    ),
                                    "edge_estimate": float(trade.get("edge_estimate", 0)),
                                    "confidence": float(trade.get("confidence", 0.5)),
                                    "category": trade.get("category", "other"),
                                }
                            )
                except json.JSONDecodeError:
                    logger.warning("Failed to parse easy trade JSON response")

        # Sort by ease_score descending
        results.sort(key=lambda x: x.get("ease_score", 0), reverse=True)
        return results[:max_results]

    async def quick_group_analysis(
        self,
        event_title: str,
        sub_markets: list,
        event_volume: float = 0,
        event_liquidity: float = 0,
        event_slug: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Quick event-level analysis for grouped trades (multiple sub-markets).
        Uses one AnalyzeMarket call with include_research=False and strict JSON output.
        """
        from app.grpc_clients import analysis_pb2

        prompts = _load_prompts()

        system_prompt = (
            prompts.get("opportunity_analysis", {}).get("system_prompt", "")
            or "You are an expert prediction market analyst. Respond with ONLY valid JSON."
        )

        table_lines = [
            "| Option | YES Price | NO Price | 24h Volume | Liquidity |",
            "|--------|-----------|----------|------------|-----------|",
        ]
        for sm in sub_markets[:20]:
            label = sm.get("label", sm.get("question", "Unknown"))
            yes_p = float(sm.get("yes_price", 0.5) or 0.5)
            no_p = float(sm.get("no_price", 0.5) or 0.5)
            vol = float(sm.get("volume_24h", 0) or 0)
            liq = float(sm.get("liquidity", 0) or 0)
            table_lines.append(
                f"| {label} | {yes_p * 100:.1f}¢ | {no_p * 100:.1f}¢ | ${vol:,.0f} | ${liq:,.0f} |"
            )
        sub_markets_table = "\n".join(table_lines)

        quick_template = prompts.get("opportunity_analysis", {}).get("event_quick_analysis", "")
        if quick_template:
            enriched_desc = quick_template.format(
                event_title=event_title,
                event_slug=event_slug or "unknown",
                sub_markets_table=sub_markets_table,
                event_volume=f"{event_volume:,.0f}",
                event_liquidity=f"{event_liquidity:,.0f}",
            )
        else:
            enriched_desc = (
                f"Event: {event_title}\nSlug: {event_slug or 'unknown'}\n\n"
                f"{sub_markets_table}\n\n"
                "Respond with JSON only: {"
                '"analysis": "2-3 sentence grouped analysis", '
                '"recommended_option": "best option label", '
                '"recommended_side": "YES or NO"'
                "}"
            )

        full_description = f"{system_prompt}\n\n{enriched_desc}"

        representative = sub_markets[0] if sub_markets else {}
        if sub_markets:
            try:
                representative = max(
                    sub_markets,
                    key=lambda sm: float(sm.get("yes_price", 0.0) or 0.0),
                )
            except Exception:
                representative = sub_markets[0]

        yes_price = float(representative.get("yes_price", 0.5) or 0.5)
        no_price = float(representative.get("no_price", 0.5) or 0.5)

        stub = await self._get_stub()
        request_kwargs = {
            "market_title": event_title,
            "market_description": full_description,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume_24h": float(event_volume or 0),
            "end_date": "",
            "include_research": False,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        raw = ""
        try:
            response = await stub.AnalyzeMarket(
                request,
                timeout=settings.grpc_timeout * 2,
            )
            raw = response.analysis if response.success else ""
        except Exception:
            raw = ""

        parsed = {}
        if raw:
            json_match = re.search(r"\{[\s\S]*\}", raw)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                except json.JSONDecodeError:
                    parsed = {}

        analysis_text = str(parsed.get("analysis", "")).strip()
        if not analysis_text:
            analysis_text = raw.strip() if raw else "Quick analysis unavailable"

        return {
            "analysis": analysis_text,
            "recommended_option": str(parsed.get("recommended_option", "")).strip(),
            "recommended_side": str(parsed.get("recommended_side", "")).strip(),
        }

    # ── NEW: Sentiment Analysis ────────────────────────────────

    async def analyze_sentiment(
        self,
        query: str,
        include_news: bool = True,
        include_social: bool = True,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Analyse public sentiment around a market/topic via the gRPC
        AnalyzeSentiment RPC.

        Returns structured dict with: sentiment_score, label, summary,
        bullish_factors, bearish_factors, etc.
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()
        proto_config = _build_llm_config(llm_config)

        request = analysis_pb2.SentimentAnalysisRequest(
            query=query,
            include_news=include_news,
            include_social=include_social,
        )
        if proto_config:
            request.llm_config.CopyFrom(proto_config)

        try:
            response = await stub.AnalyzeSentiment(request, timeout=settings.grpc_timeout * 2)
            if not response.success:
                raise RuntimeError(response.error)

            # The analysis field contains JSON-serialised result
            try:
                result = json.loads(response.analysis)
            except json.JSONDecodeError:
                result = {"summary": response.analysis}

            result["metadata"] = dict(response.metadata) if response.metadata else {}
            return result
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    # ── NEW: Autonomous Trade Discovery ────────────────────────

    async def discover_best_trade(
        self,
        markets: list,
        budget: float = 100.0,
        risk_tolerance: str = "medium",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Autonomous trade discovery via the gRPC DiscoverBestTrade RPC.
        Sends a batch of active markets and receives the AI's best trade recommendation.

        Returns structured dict with best_trade, all_candidates, etc.
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()
        proto_config = _build_llm_config(llm_config)

        market_infos = []
        for m in markets:
            prices = m.get("outcomePrices", [])
            try:
                yes_p = float(prices[0]) if prices else float(m.get("yes_price", 0.5))
                no_p = float(prices[1]) if len(prices) > 1 else 1.0 - yes_p
            except (ValueError, TypeError, IndexError):
                yes_p, no_p = 0.5, 0.5

            market_infos.append(
                analysis_pb2.MarketInfo(
                    title=str(m.get("question", m.get("title", ""))),
                    description=str(m.get("description", ""))[:500],
                    yes_price=yes_p,
                    no_price=no_p,
                    volume_24h=float(m.get("volume_24h", m.get("volume24hr", 0)) or 0),
                    end_date=str(m.get("end_date", m.get("endDate", "")) or ""),
                    market_id=str(
                        m.get("condition_id", m.get("conditionId", m.get("market_id", ""))) or ""
                    ),
                )
            )

        request = analysis_pb2.DiscoverBestTradeRequest(
            markets=market_infos,
            budget=budget,
            risk_tolerance=risk_tolerance,
        )
        if proto_config:
            request.llm_config.CopyFrom(proto_config)

        try:
            response = await stub.DiscoverBestTrade(
                request,
                timeout=settings.grpc_timeout * 5,  # Long-running
            )
            if not response.success:
                raise RuntimeError(response.error)

            try:
                result = json.loads(response.analysis)
            except json.JSONDecodeError:
                result = {"analysis": response.analysis}

            result["metadata"] = dict(response.metadata) if response.metadata else {}
            return result
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e

    # ── NEW: RAG Index ─────────────────────────────────────────

    async def index_markets_rag(
        self,
        markets: list,
        force_reindex: bool = False,
    ) -> dict[str, Any]:
        """
        Send markets to the LLM-Chain service for ChromaDB RAG indexing.
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()
        request = analysis_pb2.IndexMarketsRAGRequest(
            markets_json=json.dumps(markets),
            force_reindex=force_reindex,
        )

        try:
            response = await stub.IndexMarketsRAG(request, timeout=settings.grpc_timeout)
            return {
                "success": response.success,
                "message": response.analysis,
                "metadata": dict(response.metadata) if response.metadata else {},
            }
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}") from e


# Client factory for dependency injection
def get_analysis_client(backend: AIBackend = None) -> AnalysisClient:
    """
    Factory function to get an analysis client.

    Args:
        backend: Optional backend override

    Returns:
        AnalysisClient instance
    """
    return AnalysisClient(backend=backend)


async def close_shared_channels():
    """Close all pooled gRPC channels during application shutdown."""
    lock = _get_channels_lock()
    async with lock:
        channels = list(_shared_channels.values())
        _shared_channels.clear()
        _shared_stubs.clear()
    for channel in channels:
        await channel.close()
