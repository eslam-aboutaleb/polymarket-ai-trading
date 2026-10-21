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


def _parse_ai_json(raw: str, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extract the first JSON object from a free-text LLM response.

    Every structured-verdict path in this client smuggles a JSON-schema
    prompt through ``AnalyzeMarket.market_description`` and parses the
    object back out of the response text. This is the single shared
    implementation of that idiom so parse behaviour stays identical
    across call sites.

    Args:
        raw: Raw LLM response text (may contain prose around the JSON).
        defaults: Fallback dict returned when parsing fails.

    Returns:
        The parsed dict, or a copy of ``defaults`` (empty when omitted).
    """
    if not raw:
        return dict(defaults or {})
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        return dict(defaults or {})
    try:
        parsed = json.loads(json_match.group())
    except json.JSONDecodeError:
        return dict(defaults or {})
    if not isinstance(parsed, dict):
        return dict(defaults or {})
    return parsed


# Sentinel verdict for a price-alert response that carried no usable
# JSON. It is not one of the prompt's verdicts: it tells
# ``price_alert_service`` the answer was a fail-open default, so the
# failure is counted instead of being persisted as a real "hold".
UNUSABLE_VERDICT = "unknown"


def _opt_float(value: Any) -> float | None:
    """Coerce to a float strictly inside (0, 1); ``None`` when unusable.

    Used for ``ai_probability`` so a missing or malformed forecast stays
    absent (and is excluded from calibration aggregates) instead of
    silently defaulting to a fake probability.

    The strict interval is deliberate: a stated certainty of 0 or 1 is a
    model claiming knowledge of an unresolved market, which is not a
    forecast and cannot be scored against a resolution, so it is dropped
    rather than clamped. ``forecast_recorder`` shares this rule verbatim
    and filters its aggregates on ``ai_probability IS NOT NULL`` instead
    of backfilling a neutral 0.5.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 < num < 1.0:
        return num
    return None


def _entry_number(value: Any, default: float) -> float:
    """Coerce one numeric field of a parsed LLM entry.

    ``None`` (the model omitted the field, or emitted an explicit
    ``null``) takes the field's documented default. Anything else that
    is not a number raises: the caller drops the whole entry rather
    than letting a fabricated value reach the ranking or the route's
    merge. A numeric string is accepted, because the model is free to
    quote ``"85"`` inside its JSON.
    """
    if value is None:
        return default
    return float(value)


def _response_metadata(response: Any) -> dict[str, str]:
    """Read the proto metadata map off a response (model/provider echo)."""
    try:
        return {str(k): str(v) for k, v in dict(response.metadata or {}).items()}
    except Exception:
        return {}


def _parse_x_source(raw: Any) -> dict[str, Any]:
    """Parse the indexed-web provenance carried in the response metadata.

    The social summary of the inverse bot comes from a search-engine index, so
    the AI service reports which source it read, how complete it is, whether
    the provider answered and when it was checked. A missing or malformed
    payload yields an empty dict, and the caller falls back to the
    known-constant label.
    """
    if isinstance(raw, dict):
        parsed: Any = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items()}


def _parse_trailing_probability(analysis_text: str, meta: dict[str, Any]) -> float | None:
    """Extract ``AI Probability: <0-100>`` from a copy-trade evaluation.

    The prompt ends with three parseable trailer lines
    (Recommendation / Confidence / AI Probability). Returns a float in
    (0, 1), or None when the model omitted the line — never a fake
    default, so calibration aggregates can exclude it.
    """
    raw = meta.get("ai_probability") or meta.get("probability")
    if raw is None and analysis_text:
        match = re.search(r"AI Probability:\s*([0-9]+(?:\.[0-9]+)?)", analysis_text)
        if match:
            raw = match.group(1)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1.0:  # prompt emits a 0-100 percentage
        value = value / 100.0
    if 0.0 < value < 1.0:
        return value
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
            result["ai_probability"] = _parse_trailing_probability(result.get("analysis", ""), meta)
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
            result["x_source"] = _parse_x_source(meta.get("x_source", ""))
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

        raw = ""
        metadata: dict[str, str] = {}
        try:
            response = await stub.AnalyzeMarket(request, timeout=settings.grpc_timeout * 2)
            raw = response.analysis if response.success else ""
            metadata = _response_metadata(response)
        except Exception:
            raw = ""

        # Parse JSON from LLM response
        scored = _parse_ai_json(raw)

        return {
            "condition_id": condition_id,
            "market_title": market_title,
            "ai_score": int(scored.get("ai_score", 50)),
            "ai_probability": _opt_float(scored.get("ai_probability")),
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
            "model": metadata.get("model", ""),
            "provider": metadata.get("provider", "") or metadata.get("backend", ""),
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

        raw = ""
        metadata: dict[str, str] = {}
        try:
            response = await stub.AnalyzeMarket(request, timeout=settings.grpc_timeout * 2)
            raw = response.analysis if response.success else ""
            metadata = _response_metadata(response)
        except Exception:
            raw = ""

        scored = _parse_ai_json(raw)

        return {
            "event_title": event_title,
            "ai_score": int(scored.get("ai_score", 50)),
            "ai_probability": _opt_float(scored.get("ai_probability")),
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
            "model": metadata.get("model", ""),
            "provider": metadata.get("provider", "") or metadata.get("backend", ""),
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
        parsed = _parse_ai_json(raw)
        if raw and not parsed:
            logger.warning("Failed to parse easy trade JSON response")
        if parsed:
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
                            "expected_probability": float(trade.get("expected_probability", 0.5)),
                            "edge_estimate": float(trade.get("edge_estimate", 0)),
                            "confidence": float(trade.get("confidence", 0.5)),
                            "category": trade.get("category", "other"),
                        }
                    )

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
        metadata: dict[str, str] = {}
        try:
            response = await stub.AnalyzeMarket(
                request,
                timeout=settings.grpc_timeout * 2,
            )
            raw = response.analysis if response.success else ""
            metadata = _response_metadata(response)
        except Exception:
            raw = ""

        parsed = _parse_ai_json(raw)

        analysis_text = str(parsed.get("analysis", "")).strip()
        if not analysis_text:
            analysis_text = raw.strip() if raw else "Quick analysis unavailable"

        return {
            "analysis": analysis_text,
            "recommended_option": str(parsed.get("recommended_option", "")).strip(),
            "recommended_side": str(parsed.get("recommended_side", "")).strip(),
            "ai_probability": _opt_float(parsed.get("ai_probability")),
            "model": metadata.get("model", ""),
            "provider": metadata.get("provider", "") or metadata.get("backend", ""),
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

    # ── Daily Portfolio Digest (plan 02) ─────────────────────────

    async def generate_daily_digest(
        self,
        date: str,
        start_value: str,
        current_value: str,
        positions_json: str,
        events: str,
        llm_config: dict[str, Any] | None = None,
    ) -> str:
        """
        Render the daily portfolio digest prompt and return article prose.

        One AnalyzeMarket call with include_research=False (the digest is
        a summary product, not a research product) with the fully-rendered
        prompt smuggled through market_description.

        Tier note: like every other structured-verdict path here, the
        prompt has no dedicated RPC and rides AnalyzeMarket, so it
        currently runs at ``market_assessment``'s STRONG tier (see
        ``TASK_TIERS``) until one exists.
        """
        from app.grpc_clients import analysis_pb2

        prompts = _load_prompts()
        template = prompts.get("monitoring", {}).get("daily_portfolio_summary", "")
        if template:
            enriched_desc = template.format(
                date=date,
                start_value=start_value,
                current_value=current_value,
                positions=positions_json,
                events=events,
            )
        else:
            enriched_desc = (
                f"Date: {date}\n\nStart value: {start_value}\n"
                f"Current value: {current_value}\n\nPositions:\n{positions_json}\n\n"
                f"Events:\n{events}\n\n"
                "Write a concise daily portfolio report: P&L summary, key movers, "
                "upcoming catalysts, and 2-3 actionable points for tomorrow."
            )

        stub = await self._get_stub()
        request_kwargs = {
            "market_title": f"Daily Portfolio Digest — {date}",
            "market_description": enriched_desc,
            "yes_price": 0.5,
            "no_price": 0.5,
            "volume_24h": 0,
            "end_date": "",
            "include_research": False,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        try:
            response = await stub.AnalyzeMarket(
                request,
                timeout=settings.grpc_timeout * 2,
            )
            text = response.analysis if response.success else ""
        except Exception:
            text = ""
        return text.strip()

    # ── Price-Alert AI Verdicts (plan 03) ────────────────────────

    async def analyze_price_alert(
        self,
        market_title: str,
        position: str,
        trigger_condition: str,
        current_price: str,
        price_change: str,
        condition_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate an LLM verdict for a triggered price alert.

        Renders the monitoring.price_alert_analysis prompt (JSON schema:
        verdict / thesis_changed / confidence / driver / reasoning) through
        AnalyzeMarket with include_research=True — the prompt needs news
        context to distinguish a news-driven move from noise.

        Tier note: with no dedicated RPC, the prompt is smuggled through
        AnalyzeMarket's ``market_description`` like the other structured
        verdict paths, so this call currently runs at ``market_assessment``'s
        STRONG tier (see ``TASK_TIERS``) until a price-alert RPC exists.

        Fail-open: on any error the caller receives verdict="hold" with
        confidence 0.0 so the un-enriched alert still fires. When the
        response carries no usable JSON at all, the verdict is
        ``UNUSABLE_VERDICT`` instead, so ``price_alert_service`` counts a
        failure rather than persisting the default "hold" as if the model
        had actually recommended it.
        """
        from app.grpc_clients import analysis_pb2

        prompts = _load_prompts()
        template = prompts.get("monitoring", {}).get("price_alert_analysis", "")
        if template:
            enriched_desc = template.format(
                market_title=market_title,
                position=position,
                trigger_condition=trigger_condition,
                current_price=current_price,
                price_change=price_change,
            )
        else:
            enriched_desc = (
                f"Market: {market_title}\nPosition: {position}\n"
                f"Trigger: {trigger_condition}\nCurrent price: ${current_price}\n"
                f"Price change: {price_change}%\n\n"
                'Respond with ONLY JSON: {"verdict": "hold"|"add"|"reduce"|"exit", '
                '"thesis_changed": false, "confidence": 0.0, '
                '"driver": "news"|"technical"|"mixed", "reasoning": "..."}'
            )

        stub = await self._get_stub()
        request_kwargs = {
            "market_title": market_title,
            "market_description": enriched_desc,
            "yes_price": 0.5,
            "no_price": 0.5,
            "volume_24h": 0,
            "end_date": "",
            "include_research": True,
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

        parsed = _parse_ai_json(raw)
        if not parsed:
            # Empty or unparseable answer: fail open with the documented
            # defaults, but mark them unusable so the caller can tell a
            # silent default from the model's own recommendation.
            return {
                "condition_id": condition_id,
                "verdict": UNUSABLE_VERDICT,
                "thesis_changed": False,
                "confidence": 0.0,
                "driver": "unknown",
                "reasoning": "",
            }
        verdict = str(parsed.get("verdict", "hold")).lower()
        if verdict not in ("hold", "add", "reduce", "exit"):
            verdict = "hold"
        return {
            "condition_id": condition_id,
            "verdict": verdict,
            "thesis_changed": bool(parsed.get("thesis_changed", False)),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "driver": str(parsed.get("driver", "mixed")),
            "reasoning": str(parsed.get("reasoning", "")),
        }

    # ── Portfolio Risk Report (plan 04) ──────────────────────────

    async def generate_risk_report(
        self,
        positions_json: str,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate the AI half of the portfolio risk report.

        Renders risk_management.portfolio_risk_report (prose sections)
        through AnalyzeMarket with include_research=False. Returns the
        raw report text; deterministic metrics are computed by the caller.
        """
        from app.grpc_clients import analysis_pb2

        prompts = _load_prompts()
        template = prompts.get("risk_management", {}).get("portfolio_risk_report", "")
        if template:
            enriched_desc = template.format(positions_json=positions_json)
        else:
            enriched_desc = (
                f"Analyze the user's portfolio risk:\n\nPositions: {positions_json}\n\n"
                "Provide: overall risk rating, VaR estimate, concentration flags, "
                "correlated exposure, upcoming catalysts, tail-risk scenarios, hedges."
            )

        stub = await self._get_stub()
        request_kwargs = {
            "market_title": "Portfolio Risk Report",
            "market_description": enriched_desc,
            "yes_price": 0.5,
            "no_price": 0.5,
            "volume_24h": 0,
            "end_date": "",
            "include_research": False,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        try:
            response = await stub.AnalyzeMarket(
                request,
                timeout=settings.grpc_timeout * 3,
            )
            text = response.analysis if response.success else ""
        except Exception:
            text = ""
        return {"report": text.strip(), "success": bool(text.strip())}

    # ── Event-group Easy Trades (plan 06) ───────────────────────

    async def scan_event_easy_trades(
        self,
        events: list,
        max_results: int = 10,
        llm_config: dict[str, Any] | None = None,
    ) -> list:
        """
        Batch-scan EVENT GROUPS to identify the easiest/most obvious
        per-option trades (the event-level counterpart of
        ``scan_easy_trades``).

        Sends all event groups in ONE AnalyzeMarket call with
        include_research=False (easy trades are common-knowledge scans,
        not research products) and returns a ranked list. Each entry
        keeps its sub-market ``condition_id`` so the frontend can merge
        the score onto the specific option inside the group.
        """
        from app.grpc_clients import analysis_pb2

        prompts = _load_prompts()
        system_prompt = (
            prompts.get("easy_trade_analysis", {}).get("system_prompt", "")
            or "You are an expert prediction market analyst. Respond with ONLY valid JSON."
        )
        scoring_template = prompts.get("easy_trade_analysis", {}).get("event_batch_scoring", "")

        # Build a markdown table of event groups (sub-markets as rows).
        table_lines = [
            "| Event | Option | Condition ID | YES Price | NO Price | 24h Volume | Liquidity |",
            "|-------|--------|--------------|-----------|----------|------------|-----------|",
        ]
        for event in events[:20]:
            if not isinstance(event, dict):
                continue
            title = str(event.get("event_title") or event.get("title") or "Unknown")[:70]
            sub_markets = event.get("sub_markets") or []
            for sm in sub_markets[:20]:
                if not isinstance(sm, dict):
                    continue
                label = sm.get("label", sm.get("question", "Unknown"))
                cid = sm.get("condition_id", "")
                # Defensive coercion: a malformed price or volume must
                # not abort the whole scan, so the row falls back to
                # the same neutral defaults ``scan_easy_trades`` uses.
                try:
                    yes_p = float(sm.get("yes_price", 0.5) or 0.5)
                    no_p = float(sm.get("no_price", 0.5) or 0.5)
                    vol = float(sm.get("volume_24h", 0) or 0)
                    liq = float(sm.get("liquidity", 0) or 0)
                except (TypeError, ValueError):
                    logger.debug("Event easy-trade scan: skipping malformed row %r", sm)
                    yes_p, no_p, vol, liq = 0.5, 0.5, 0.0, 0.0
                table_lines.append(
                    f"| {title} | {label} | {cid} | {yes_p * 100:.1f}¢ "
                    f"| {no_p * 100:.1f}¢ | ${vol:,.0f} | ${liq:,.0f} |"
                )
        events_table = "\n".join(table_lines)

        if scoring_template:
            enriched_desc = scoring_template.format(
                events_table=events_table,
                max_results=max_results,
            )
        else:
            enriched_desc = (
                f"NEWEST EVENT GROUPS:\n{events_table}\n\n"
                f"Return the top easiest trades (up to {max_results}) as JSON: "
                '{"easy_trades": [{"condition_id": "", "market_title": "", '
                '"recommended_option": "", "ease_score": 50, "recommended_side": "YES", '
                '"ai_probability": 0.5, "expected_probability": 0.5, "edge_estimate": 0, '
                '"confidence": 0.5, "reasoning": "", "risk_level": "medium", '
                '"category": "other"}], "scan_summary": ""}'
            )

        full_description = f"{system_prompt}\n\n{enriched_desc}"

        stub = await self._get_stub()
        first_price = 0.5
        for event in events:
            if not isinstance(event, dict):
                continue
            for sm in event.get("sub_markets") or []:
                try:
                    first_price = float(sm.get("yes_price", 0.5) or 0.5)
                except (TypeError, ValueError, AttributeError):
                    first_price = 0.5
                break
            break
        request_kwargs = {
            "market_title": f"Event Easy Trade Scan — {len(events)} event groups",
            "market_description": full_description,
            "yes_price": first_price,
            "no_price": 1.0 - first_price,
            "volume_24h": 0,
            "end_date": "",
            "include_research": False,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        raw = ""
        metadata: dict[str, str] = {}
        try:
            response = await stub.AnalyzeMarket(
                request,
                timeout=settings.grpc_timeout * 2,
            )
            raw = response.analysis if response.success else ""
            metadata = _response_metadata(response)
        except Exception as e:
            logger.error("Event easy trade scan gRPC failed: %s", e)
            raw = ""

        results = []
        parsed = _parse_ai_json(raw)
        if raw and not parsed:
            logger.warning("Failed to parse event easy trade JSON response")
        if parsed:
            easy_trades = parsed.get("easy_trades", [])
            if isinstance(easy_trades, list):
                for trade in easy_trades:
                    if not isinstance(trade, dict):
                        continue
                    # One malformed entry must not abort the scan row (the
                    # route swallows the exception and loses every other
                    # group's score), so each numeric coercion is guarded.
                    # Junk drops the entry: ``ease_score`` is the ranking key
                    # and the value the route merges onto the option, so
                    # defaulting it would inject a fabricated middle-ranked
                    # row. A missing or null field still takes its default.
                    try:
                        ease = int(_entry_number(trade.get("ease_score"), 50))
                        edge = _entry_number(trade.get("edge_estimate"), 0)
                        confidence = _entry_number(trade.get("confidence"), 0.5)
                        expected_probability = _entry_number(
                            trade.get("expected_probability"),
                            0.5,
                        )
                    except (TypeError, ValueError):
                        logger.warning(
                            "Event easy trade scan: skipping entry with a non-numeric field (%r)",
                            trade,
                        )
                        continue
                    results.append(
                        {
                            "condition_id": trade.get("condition_id", ""),
                            "market_title": trade.get("market_title", ""),
                            "ai_score": ease,
                            "ai_probability": _opt_float(trade.get("ai_probability")),
                            "ease_score": ease,
                            "risk_level": trade.get("risk_level", "medium"),
                            "pnl_potential": abs(edge),
                            "credibility_score": int(confidence * 100),
                            "smart_money_signal": "neutral",
                            "smart_money_summary": "",
                            "recommendation": (
                                "strong_buy"
                                if ease >= 80
                                else "buy"
                                if ease >= 60
                                else "hold"
                                if ease >= 40
                                else "avoid"
                            ),
                            "recommended_side": trade.get("recommended_side", "YES"),
                            "recommended_option": trade.get("recommended_option", ""),
                            "reasoning": trade.get("reasoning", ""),
                            "search_summary": parsed.get("scan_summary", ""),
                            "key_risks": [],
                            "expected_probability": expected_probability,
                            "edge_estimate": edge,
                            "confidence": confidence,
                            "category": trade.get("category", "other"),
                            "model": metadata.get("model", ""),
                            "provider": metadata.get("provider", "") or metadata.get("backend", ""),
                        }
                    )

        results.sort(key=lambda x: x.get("ease_score", 0), reverse=True)
        return results[:max_results]

    # ── Research Credibility Synthesis (plan 06) ────────────────

    async def synthesize_research(
        self,
        market_question: str,
        findings_json: str,
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Synthesize a batch of research findings into a credibility-aware
        analysis (consensus, disagreements, base-rate anchor, calibrated
        probability).

        One AnalyzeMarket call with include_research=False and the
        fully-rendered prompt smuggled through market_description.
        """
        from app.grpc_clients import analysis_pb2

        prompts = _load_prompts()
        template = prompts.get("research", {}).get("synthesize_research", "")
        if template:
            enriched_desc = template.format(
                market_question=market_question,
                research_findings=findings_json,
            )
        else:
            enriched_desc = (
                f"Synthesize the research findings for: {market_question}\n\n"
                f"Findings:\n{findings_json}\n\n"
                "Provide: key consensus, disagreements, base-rate anchor, "
                "calibrated probability, information gaps."
            )

        stub = await self._get_stub()
        request_kwargs = {
            "market_title": f"Research synthesis — {market_question[:80]}",
            "market_description": enriched_desc,
            "yes_price": 0.5,
            "no_price": 0.5,
            "volume_24h": 0,
            "end_date": "",
            "include_research": False,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config

        request = analysis_pb2.MarketAnalysisRequest(**request_kwargs)

        try:
            response = await stub.AnalyzeMarket(
                request,
                timeout=settings.grpc_timeout * 2,
            )
            text = response.analysis if response.success else ""
        except Exception:
            text = ""
        return {"report": text.strip(), "success": bool(text.strip())}

    # ── Multi-step Orchestration (plan 01) ──────────────────────

    async def orchestrate(
        self,
        intent: str,
        payload: dict[str, Any] | None = None,
        steps: list[str] | None = None,
        user_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Run a multi-step orchestration plan on the llm-chain backend.

        The planner, per-step routing and synthesis all live in the service
        (see ``services/llm-chain/src/orchestrator.py``); this method only
        transports the intent + payload and the request-level LLM config,
        which is what decides whether each step runs on llm-chain or on
        cli-agent (GitHub Models providers).

        Fail-soft: a failed step yields ``partial=True`` with a typed
        ``error`` per step, and the synthesis is still returned. Only a
        transport failure raises.

        Attribution: ``prompt_version`` / ``experiment_id`` / ``variant`` /
        ``llm_tier`` are not fields of the proto LLMConfig, so they ride
        along inside ``llm_config`` and are stamped onto the returned
        ``metadata`` for the verdict recorders (plan 04).
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()
        request_kwargs: dict[str, Any] = {
            "intent": intent,
            "payload_json": json.dumps(payload or {}, default=str),
            "steps": steps or [],
            "user_id": user_id,
            "stream": False,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config
        request = analysis_pb2.OrchestrateRequest(**request_kwargs)

        try:
            response = await stub.OrchestrateAnalysis(
                request,
                timeout=settings.grpc_timeout * ORCHESTRATION_TIMEOUT_MULTIPLIER,
            )
        except Exception as exc:
            raise RuntimeError(f"OrchestrateAnalysis failed: {exc}") from exc

        return _orchestrate_result(response, llm_config)

    async def orchestrate_stream(
        self,
        intent: str,
        payload: dict[str, Any] | None = None,
        steps: list[str] | None = None,
        user_id: str = "",
        llm_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream an orchestration plan: one ``step`` event per executed step,
        then a final ``recommendation`` event with the synthesis.

        Yields the decoded event dicts from the service; a transport failure
        yields a single ``error`` event instead of raising so the SSE route
        can render it with the existing error envelope.
        """
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()
        request_kwargs: dict[str, Any] = {
            "intent": intent,
            "payload_json": json.dumps(payload or {}, default=str),
            "steps": steps or [],
            "user_id": user_id,
            "stream": True,
        }
        proto_llm_config = _build_llm_config(llm_config)
        if proto_llm_config is not None:
            request_kwargs["llm_config"] = proto_llm_config
        request = analysis_pb2.OrchestrateRequest(**request_kwargs)

        try:
            async for chunk in stub.OrchestrateAnalysisStream(
                request,
                timeout=settings.grpc_timeout * ORCHESTRATION_TIMEOUT_MULTIPLIER,
            ):
                event = _parse_ai_json(chunk.chunk) or {"raw": chunk.chunk}
                if not isinstance(event, dict):  # pragma: no cover - defensive
                    event = {"raw": chunk.chunk}
                event.setdefault("chunk_type", chunk.chunk_type)
                event["is_final"] = chunk.is_final
                yield event
                if chunk.is_final and chunk.chunk_type == "recommendation":
                    break
        except Exception as exc:
            logger.error("OrchestrateAnalysisStream failed: %s", exc)
            yield {"chunk_type": "error", "error": "Stream processing failed", "is_final": True}


# ── Orchestration helpers ─────────────────────────────────────

#: A plan runs several STRONG-tier LLM calls in sequence, so it needs a much
#: longer budget than a single analysis RPC.
ORCHESTRATION_TIMEOUT_MULTIPLIER = 10

#: Keys that are not proto LLMConfig fields but must survive the round-trip
#: so recorded verdicts stay attributable (prompt version, A/B arm, tier).
ORCHESTRATION_ATTRIBUTION_KEYS = (
    "prompt_version",
    "experiment_id",
    "variant",
    "llm_tier",
)


def _orchestrate_result(response: Any, llm_config: dict[str, Any] | None) -> dict[str, Any]:
    """Decode an ``OrchestrateResponse`` into the route-facing dict."""
    steps: list[dict[str, Any]] = []
    for step in response.steps:
        output: dict[str, Any] = {}
        if step.output_json:
            parsed = _parse_ai_json(step.output_json)
            output = parsed if isinstance(parsed, dict) else {"raw": step.output_json}
        steps.append(
            {
                "step": step.step,
                "ok": step.ok,
                "output": output,
                "error": step.error,
                "backend": step.backend,
                "duration_ms": step.duration_ms,
            }
        )
    metadata = dict(response.metadata)
    for key in ORCHESTRATION_ATTRIBUTION_KEYS:
        value = (llm_config or {}).get(key)
        if value:
            metadata.setdefault(key, str(value))
    return {
        "success": response.success,
        "synthesis": response.synthesis,
        "steps": steps,
        "partial": response.partial,
        "metadata": metadata,
    }


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
