"""
Analysis Service gRPC Client
Provides a unified interface to communicate with AI backend services.
"""
import asyncio
import grpc
import json
import os
from typing import Dict, Any, AsyncIterator, Optional
from functools import lru_cache

from app.config import get_settings, AIBackend

# These will be generated from proto
# Import after generation: from app.grpc_clients import analysis_pb2, analysis_pb2_grpc

settings = get_settings()
_shared_channels: Dict[str, grpc.aio.Channel] = {}
_shared_stubs: Dict[str, Any] = {}
_shared_channels_lock = asyncio.Lock()


@lru_cache(maxsize=1)
def _load_prompts() -> Dict[str, Any]:
    """Load prompts from common runtime locations."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "prompts", "prompts.json"),
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts", "prompts.json"),
        "/app/prompts/prompts.json",
    ]
    for path in candidates:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


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
        else:
            return f"{settings.cli_agent_grpc_host}:{settings.cli_agent_grpc_port}"
    
    async def _get_stub(self):
        """Get or create gRPC stub."""
        if self._stub is None:
            try:
                # Import generated protobuf modules
                from app.grpc_clients import analysis_pb2_grpc
                addr = self.service_address

                async with _shared_channels_lock:
                    if addr not in _shared_channels:
                        channel = grpc.aio.insecure_channel(
                            addr,
                            options=[
                                ('grpc.max_receive_message_length', 50 * 1024 * 1024),
                                ('grpc.keepalive_time_ms', 10000),
                            ]
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
                )
        return self._stub
    
    async def analyze_market(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True
    ) -> Dict[str, Any]:
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
        
        Returns:
            Analysis result dictionary
        """
        from app.grpc_clients import analysis_pb2
        
        stub = await self._get_stub()
        
        request = analysis_pb2.MarketAnalysisRequest(
            market_title=market_title,
            market_description=market_description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            end_date=end_date,
            include_research=include_research
        )
        
        try:
            response = await stub.AnalyzeMarket(
                request,
                timeout=settings.grpc_timeout
            )
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")
    
    async def quick_analysis(
        self,
        question: str,
        current_price: float
    ) -> Dict[str, Any]:
        """
        Quick 2-3 sentence market analysis.
        
        Args:
            question: Market question
            current_price: Current market price (0-1)
        
        Returns:
            Quick analysis result
        """
        from app.grpc_clients import analysis_pb2
        
        stub = await self._get_stub()
        
        request = analysis_pb2.QuickAnalysisRequest(
            question=question,
            current_price=current_price
        )
        
        try:
            response = await stub.QuickAnalysis(
                request,
                timeout=settings.grpc_timeout
            )
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")
    
    async def scan_markets(
        self,
        markets: list
    ) -> Dict[str, Any]:
        """
        Scan multiple markets to identify opportunities.
        
        Args:
            markets: List of market dictionaries
        
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
        
        try:
            response = await stub.ScanMarkets(
                request,
                timeout=settings.grpc_timeout
            )
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")
    
    async def assess_risk(
        self,
        market_title: str,
        position_size: float,
        entry_price: float,
        days_to_expiry: int,
        correlation_info: str = ""
    ) -> Dict[str, Any]:
        """
        Assess risk for a potential position.
        
        Args:
            market_title: Market title
            position_size: Position size in USD
            entry_price: Entry price (0-1)
            days_to_expiry: Days until market expiry
            correlation_info: Optional correlation information
        
        Returns:
            Risk assessment result
        """
        from app.grpc_clients import analysis_pb2
        
        stub = await self._get_stub()
        
        request = analysis_pb2.RiskAssessmentRequest(
            market_title=market_title,
            position_size=position_size,
            entry_price=entry_price,
            days_to_expiry=days_to_expiry,
            correlation_info=correlation_info
        )
        
        try:
            response = await stub.AssessRisk(
                request,
                timeout=settings.grpc_timeout
            )
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")
    
    async def generate_trade_plan(
        self,
        action: str,
        market_title: str,
        target_size: float,
        current_price: float,
        order_book: dict = None
    ) -> Dict[str, Any]:
        """
        Generate execution plan for a trade.
        
        Args:
            action: Trade action (buy_yes, buy_no, sell_yes, sell_no)
            market_title: Market title
            target_size: Target size in USD
            current_price: Current price (0-1)
            order_book: Optional order book data
        
        Returns:
            Trade execution plan
        """
        from app.grpc_clients import analysis_pb2
        
        stub = await self._get_stub()
        
        request = analysis_pb2.TradePlanRequest(
            action=action,
            market_title=market_title,
            target_size=target_size,
            current_price=current_price,
            order_book_json=json.dumps(order_book or {})
        )
        
        try:
            response = await stub.GenerateTradePlan(
                request,
                timeout=settings.grpc_timeout
            )
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")
    
    async def analyze_market_stream(
        self,
        market_title: str,
        market_description: str,
        yes_price: float,
        no_price: float,
        volume_24h: float,
        end_date: str,
        include_research: bool = True
    ) -> AsyncIterator[str]:
        """
        Streaming market analysis - yields chunks as they're generated.
        
        Yields:
            Analysis text chunks
        """
        from app.grpc_clients import analysis_pb2
        
        stub = await self._get_stub()
        
        request = analysis_pb2.MarketAnalysisRequest(
            market_title=market_title,
            market_description=market_description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            end_date=end_date,
            include_research=include_research
        )
        
        try:
            async for chunk in stub.AnalyzeMarketStream(
                request,
                timeout=settings.grpc_timeout * 2
            ):
                yield chunk.chunk
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC streaming error: {e.code()}: {e.details()}")
    
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
    ) -> Dict[str, Any]:
        """Full trader profile analysis with internet research."""
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request = analysis_pb2.TraderAnalysisRequest(
            wallet_address=wallet_address,
            display_name=display_name,
            total_pnl=total_pnl,
            win_rate=win_rate,
            trade_count=trade_count,
            markets_traded=markets_traded,
            recent_trades_json=recent_trades_json,
            user_id=user_id,
        )

        try:
            response = await stub.AnalyzeTrader(
                request, timeout=settings.grpc_timeout
            )
            return self._parse_response(response)
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")

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
        )

        analysis_text = result.get("analysis", "")
        if not analysis_text:
            yield "No analysis available for this trader."
            return

        # Stream in sentence-like chunks for a real-time feel
        import re as _re
        sentences = _re.split(r'(?<=[.!?\n])\s+', analysis_text)
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
    ) -> Dict[str, Any]:
        """Evaluate whether to copy a specific trade."""
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request = analysis_pb2.CopyTradeEvaluationRequest(
            trader_wallet=trader_wallet,
            trader_stats=trader_stats,
            market_id=market_id,
            market_title=market_title,
            trade_side=trade_side,
            trade_size=trade_size,
            current_price=current_price,
            user_risk_profile=user_risk_profile,
            user_id=user_id,
        )

        try:
            response = await stub.EvaluateCopyTrade(
                request, timeout=settings.grpc_timeout
            )
            result = self._parse_response(response)
            # Extract recommendation metadata
            meta = result.get("metadata", {})
            result["recommendation"] = meta.get("recommendation", "copy")
            result["confidence"] = float(meta.get("confidence", 50))
            result["risk_level"] = meta.get("risk_level", "medium")
            return result
        except grpc.RpcError as e:
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")

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
    ) -> Dict[str, Any]:
        """Evaluate whether to reverse an open position."""
        from app.grpc_clients import analysis_pb2

        stub = await self._get_stub()

        request = analysis_pb2.InversePositionEvaluationRequest(
            condition_id=condition_id,
            market_title=market_title,
            held_outcome=held_outcome,
            held_pct=held_pct,
            best_alt_outcome=best_alt_outcome,
            best_alt_pct=best_alt_pct,
            delta_pct=delta_pct,
            alternatives_json=json.dumps(alternatives_json or []),
            include_research=include_research,
            user_id=user_id,
        )

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
            raise RuntimeError(f"gRPC error: {e.code()}: {e.details()}")
    
    def _parse_response(self, response) -> Dict[str, Any]:
        """Parse gRPC response to dictionary."""
        if not response.success:
            raise RuntimeError(f"Analysis service error: {response.error}")
        result = {
            "analysis": response.analysis,
            "research_context": response.research_context,
            "metadata": dict(response.metadata) if response.metadata else {},
            "timestamp": response.timestamp,
        }
        return result
    
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
    ) -> Dict[str, Any]:
        """
        Score a single market opportunity by leveraging the existing
        AnalyzeMarket RPC with an enriched opportunity-scoring prompt.
        Returns structured JSON scoring.
        """
        from app.grpc_clients import analysis_pb2
        import re

        # Load opportunity prompt templates.
        prompts = _load_prompts()

        system_prompt = (
            prompts.get("opportunity_analysis", {}).get("system_prompt", "")
            or "You are an expert prediction market analyst. Respond with ONLY valid JSON."
        )
        scoring_template = (
            prompts.get("opportunity_analysis", {}).get("market_scoring", "")
        )

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
        request = analysis_pb2.MarketAnalysisRequest(
            market_title=market_title,
            market_description=full_description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=volume_24h,
            end_date=end_date,
            include_research=True,
        )

        try:
            response = await stub.AnalyzeMarket(
                request, timeout=settings.grpc_timeout * 2
            )
            raw = response.analysis if response.success else ""
        except Exception as e:
            raw = ""

        # Parse JSON from LLM response
        scored = {}
        if raw:
            json_match = re.search(r'\{[\s\S]*\}', raw)
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
    ) -> Dict[str, Any]:
        """
        Score an entire event group (multiple related sub-markets) with
        ONE LLM call via the AnalyzeMarket RPC.  Returns structured JSON
        scoring that includes 'recommended_option'.
        """
        from app.grpc_clients import analysis_pb2
        import re

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
                f"Respond with JSON: ai_score, risk_level, recommendation, recommended_option, reasoning"
            )

        full_description = f"{system_prompt}\n\n{enriched_desc}"

        # Use the first sub-market's prices as representative for the RPC
        first = sub_markets[0] if sub_markets else {}
        stub = await self._get_stub()
        request = analysis_pb2.MarketAnalysisRequest(
            market_title=event_title,
            market_description=full_description,
            yes_price=first.get("yes_price", 0.5),
            no_price=first.get("no_price", 0.5),
            volume_24h=float(event_volume) if event_volume else 0,
            end_date="",
            include_research=True,
        )

        try:
            response = await stub.AnalyzeMarket(
                request, timeout=settings.grpc_timeout * 2
            )
            raw = response.analysis if response.success else ""
        except Exception as e:
            raw = ""

        scored = {}
        if raw:
            json_match = re.search(r'\{[\s\S]*\}', raw)
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

    async def quick_group_analysis(
        self,
        event_title: str,
        sub_markets: list,
        event_volume: float = 0,
        event_liquidity: float = 0,
        event_slug: str = "",
    ) -> Dict[str, Any]:
        """
        Quick event-level analysis for grouped trades (multiple sub-markets).
        Uses one AnalyzeMarket call with include_research=False and strict JSON output.
        """
        from app.grpc_clients import analysis_pb2
        import re

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

        quick_template = (
            prompts.get("opportunity_analysis", {}).get("event_quick_analysis", "")
        )
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
                "\"analysis\": \"2-3 sentence grouped analysis\", "
                "\"recommended_option\": \"best option label\", "
                "\"recommended_side\": \"YES or NO\""
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
        request = analysis_pb2.MarketAnalysisRequest(
            market_title=event_title,
            market_description=full_description,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=float(event_volume or 0),
            end_date="",
            include_research=False,
        )

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
    async with _shared_channels_lock:
        channels = list(_shared_channels.values())
        _shared_channels.clear()
        _shared_stubs.clear()
    for channel in channels:
        await channel.close()
