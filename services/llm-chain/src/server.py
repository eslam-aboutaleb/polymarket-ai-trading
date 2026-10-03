"""
gRPC server for LLM Chain Analysis Service.
Supports per-request LLM provider selection via the factory/gateway pattern.
"""

import asyncio
import contextlib
import json
import logging
from concurrent import futures
from datetime import datetime

# Import generated protobuf modules (will be generated from proto file)
import analysis_pb2
import analysis_pb2_grpc
import grpc
from grpc_reflection.v1alpha import reflection

from src.analysis_chain import get_chain_for_request, get_trading_chain
from src.config import get_settings
from src.llm_factory import get_available_providers, parse_llm_config_from_proto

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _get_chain_for_request(request):
    """
    Get the appropriate chain for a request, considering llm_config override.

    This implements the gateway pattern - routing to the right LLM provider
    based on per-request configuration.
    """
    # Check if request has llm_config field
    llm_config = getattr(request, "llm_config", None)
    request_config = parse_llm_config_from_proto(llm_config)
    return get_chain_for_request(request_config)


def _get_provider_metadata(request) -> dict:
    """Extract provider info for response metadata."""
    llm_config = getattr(request, "llm_config", None)
    if llm_config and llm_config.provider:
        # Map proto enum to string
        PROVIDER_NAMES = {
            0: "default",
            1: "openai",
            2: "anthropic",
            3: "google",
            4: "groq",
            5: "ollama",
            6: "github",
        }
        provider = PROVIDER_NAMES.get(llm_config.provider, "unknown")
        model = llm_config.model or "default"
        return {"provider": provider, "model": model}
    return {}


class AnalysisServicer(analysis_pb2_grpc.AnalysisServiceServicer):
    """Implementation of AnalysisService for LLM Chain."""

    def __init__(self):
        self.settings = get_settings()
        self.default_chain = get_trading_chain()
        logger.info(f"Initialized {self.settings.service_name} v{self.settings.service_version}")
        logger.info(f"Available providers: {list(get_available_providers().keys())}")

    async def AnalyzeMarket(self, request, context):
        """Comprehensive market analysis with optional research."""
        try:
            logger.info(f"AnalyzeMarket request for: {request.market_title}")

            # Get chain for this request (may use custom provider)
            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            result = await chain.analyze_market(
                market_title=request.market_title,
                market_description=request.market_description,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date,
                include_research=request.include_research,
            )

            metadata = {"backend": "llm-chain", "model": self.settings.llm_model}
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                research_context=result.get("research_context", ""),
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"AnalyzeMarket error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False, error=str(e), timestamp=datetime.utcnow().isoformat()
            )

    async def QuickAnalysis(self, request, context):
        """Quick 2-3 sentence analysis."""
        try:
            logger.info(f"QuickAnalysis request: {request.question}")

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            result = await chain.quick_analysis(
                question=request.question, current_price=request.current_price
            )

            metadata = {"backend": "llm-chain"}
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result,
                timestamp=datetime.utcnow().isoformat(),
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"QuickAnalysis error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False, error=str(e), timestamp=datetime.utcnow().isoformat()
            )

    async def ScanMarkets(self, request, context):
        """Scan multiple markets for opportunities."""
        try:
            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            markets = [
                {
                    "title": m.title,
                    "description": m.description,
                    "yes_price": m.yes_price,
                    "no_price": m.no_price,
                    "volume_24h": m.volume_24h,
                    "end_date": m.end_date,
                    "market_id": m.market_id,
                }
                for m in request.markets
            ]

            logger.info(f"ScanMarkets request for {len(markets)} markets")

            result = await chain.scan_markets(markets)

            metadata = {"backend": "llm-chain", "markets_scanned": str(result["markets_scanned"])}
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"ScanMarkets error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False, error=str(e), timestamp=datetime.utcnow().isoformat()
            )

    async def AssessRisk(self, request, context):
        """Assess risk for a potential trade."""
        try:
            logger.info(f"AssessRisk request for: {request.market_title}")

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            result = await chain.assess_risk(
                market_title=request.market_title,
                position_size=request.position_size,
                entry_price=request.entry_price,
                days_to_expiry=request.days_to_expiry,
                correlation_info=request.correlation_info or "No correlation data",
            )

            metadata = {"backend": "llm-chain"}
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"AssessRisk error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False, error=str(e), timestamp=datetime.utcnow().isoformat()
            )

    async def GenerateTradePlan(self, request, context):
        """Generate trade execution plan."""
        try:
            logger.info(f"GenerateTradePlan request: {request.action} on {request.market_title}")

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            order_book = None
            if request.order_book_json:
                with contextlib.suppress(json.JSONDecodeError):
                    order_book = json.loads(request.order_book_json)

            result = await chain.generate_trade_plan(
                action=request.action,
                market_title=request.market_title,
                target_size=request.target_size,
                current_price=request.current_price,
                order_book=order_book,
            )

            metadata = {"backend": "llm-chain"}
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"GenerateTradePlan error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False, error=str(e), timestamp=datetime.utcnow().isoformat()
            )

    async def AnalyzeMarketStream(self, request, context):
        """Stream market analysis for real-time updates."""
        try:
            logger.info(f"AnalyzeMarketStream request for: {request.market_title}")

            chain = _get_chain_for_request(request)

            async for chunk in chain.analyze_market_stream(
                market_title=request.market_title,
                market_description=request.market_description,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date,
                include_research=request.include_research,
            ):
                yield analysis_pb2.AnalysisChunk(
                    chunk=chunk["chunk"], is_final=chunk["is_final"], chunk_type=chunk["chunk_type"]
                )
        except Exception as e:
            logger.error(f"AnalyzeMarketStream error: {e}")
            yield analysis_pb2.AnalysisChunk(
                chunk=f"Error: {str(e)}", is_final=True, chunk_type="error"
            )

    async def AnalyzeTrader(self, request, context):
        """Full trader profile analysis with internet research."""
        try:
            logger.info(f"AnalyzeTrader request for wallet: {request.wallet_address}")

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            result = await chain.analyze_trader(
                wallet_address=request.wallet_address,
                display_name=request.display_name,
                total_pnl=request.total_pnl,
                win_rate=request.win_rate,
                trade_count=request.trade_count,
                markets_traded=request.markets_traded,
                recent_trades_json=request.recent_trades_json,
            )

            metadata = {"backend": "llm-chain", "type": "trader_analysis"}
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                research_context=result.get("research_context", ""),
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"AnalyzeTrader error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat(),
            )

    async def EvaluateCopyTrade(self, request, context):
        """Evaluate whether to copy a specific trade."""
        try:
            logger.info(
                f"EvaluateCopyTrade for trader {request.trader_wallet} on {request.market_title}"
            )

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            result = await chain.evaluate_copy_trade(
                trader_wallet=request.trader_wallet,
                trader_stats=request.trader_stats,
                market_id=request.market_id,
                market_title=request.market_title,
                trade_side=request.trade_side,
                trade_size=request.trade_size,
                current_price=request.current_price,
                user_risk_profile=request.user_risk_profile,
            )

            metadata = {
                "backend": "llm-chain",
                "type": "copy_trade_eval",
                "recommendation": result.get("recommendation", ""),
                "confidence": str(result.get("confidence", 0)),
                "risk_level": result.get("risk_level", ""),
            }
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                research_context=result.get("research_context", ""),
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"EvaluateCopyTrade error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat(),
            )

    async def EvaluateInversePosition(self, request, context):
        """Evaluate whether an open position should be reversed."""
        try:
            logger.info(
                "EvaluateInversePosition request: condition_id=%s",
                request.condition_id,
            )

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            result = await chain.evaluate_inverse_position(
                condition_id=request.condition_id,
                market_title=request.market_title,
                held_outcome=request.held_outcome,
                held_pct=request.held_pct,
                best_alt_outcome=request.best_alt_outcome,
                best_alt_pct=request.best_alt_pct,
                delta_pct=request.delta_pct,
                alternatives_json=request.alternatives_json,
                include_research=request.include_research,
            )

            metadata = {
                "backend": "llm-chain",
                "type": "inverse_position_eval",
                "recommendation": str(result.get("recommendation", "hold")),
                "confidence": str(result.get("confidence", 0)),
                "reasoning": str(result.get("reasoning", "")),
                "key_risks": json.dumps(result.get("key_risks", [])),
                "alt_outcome": str(result.get("alt_outcome", "")),
                "alt_token_id": str(result.get("alt_token_id", "")),
                "web_summary": str(result.get("web_summary", "")),
                "x_summary": str(result.get("x_summary", "")),
            }
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result.get("analysis", ""),
                research_context=result.get("web_summary", ""),
                timestamp=result.get("timestamp", datetime.utcnow().isoformat()),
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"EvaluateInversePosition error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat(),
            )

    async def HealthCheck(self, request, context):
        """Service health check."""
        return analysis_pb2.HealthResponse(
            healthy=True,
            service_name=self.settings.service_name,
            version=self.settings.service_version,
            capabilities={
                "streaming": "true",
                "web_search": "true",
                "tavily": str(bool(self.settings.tavily_enabled)).lower(),
                "newsapi": str(bool(self.settings.newsapi_enabled)).lower(),
                "rag": str(bool(self.settings.rag_enabled)).lower(),
                "model": self.settings.llm_model,
            },
        )

    async def AnalyzeSentiment(self, request, context):
        """Analyse public sentiment around a market/topic."""
        try:
            logger.info(f"AnalyzeSentiment request: {request.query}")

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            result = await chain.analyze_sentiment(
                query=request.query,
                include_news=request.include_news if request.include_news else True,
                include_social=request.include_social if request.include_social else True,
            )

            metadata = {
                "backend": "llm-chain",
                "type": "sentiment_analysis",
                "sentiment_score": str(result.get("sentiment_score", 0.5)),
                "label": result.get("label", "neutral"),
                "source_count": str(result.get("source_count", 0)),
            }
            metadata.update(provider_meta)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=json.dumps(result),
                research_context=result.get("evidence_snippet", ""),
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"AnalyzeSentiment error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat(),
            )

    async def DiscoverBestTrade(self, request, context):
        """Autonomous trade discovery — find the single best trade."""
        try:
            logger.info(
                "DiscoverBestTrade: %d markets, budget=$%.0f, risk=%s",
                len(request.markets),
                request.budget,
                request.risk_tolerance,
            )

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            markets = [
                {
                    "question": m.title,
                    "description": m.description,
                    "outcomePrices": [m.yes_price, m.no_price],
                    "volume_24h": m.volume_24h,
                    "liquidity": 0,
                    "end_date": m.end_date,
                    "conditionId": m.market_id,
                    "slug": m.market_id,
                }
                for m in request.markets
            ]

            result = await chain.discover_best_trade(
                markets=markets,
                budget=request.budget or 100.0,
                risk_tolerance=request.risk_tolerance or "medium",
            )

            metadata = {
                "backend": "llm-chain",
                "type": "auto_discovery",
                "events_evaluated": str(result.get("events_evaluated", 0)),
                "markets_evaluated": str(result.get("markets_evaluated", 0)),
            }
            metadata.update(provider_meta)

            best = result.get("best_trade", {})
            if best:
                metadata["best_side"] = best.get("side", "")
                metadata["best_edge"] = str(best.get("edge", 0))
                metadata["best_confidence"] = str(best.get("confidence", 0))
                metadata["best_condition_id"] = best.get("condition_id", "")

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=json.dumps(result),
                timestamp=result["timestamp"],
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"DiscoverBestTrade error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat(),
            )

    async def IndexMarketsRAG(self, request, context):
        """Index markets into ChromaDB RAG vector store."""
        try:
            from src.market_rag import MarketRAGService

            rag = MarketRAGService.get_instance()

            markets = json.loads(request.markets_json or "[]")
            logger.info(f"IndexMarketsRAG: indexing {len(markets)} markets")

            count = rag.index_markets(markets, force=request.force_reindex)

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=f"Indexed {count} markets into ChromaDB RAG store",
                timestamp=datetime.utcnow().isoformat(),
                metadata={
                    "indexed_count": str(count),
                    "collection_size": str(rag.get_collection_size()),
                },
            )
        except Exception as e:
            logger.error(f"IndexMarketsRAG error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat(),
            )

    async def GenerateNews(self, request, context):
        """Generate an AI news article for a single market."""
        try:
            logger.info(f"GenerateNews request for: {request.market_question}")

            chain = _get_chain_for_request(request)
            provider_meta = _get_provider_metadata(request)

            article = await chain.generate_news_article(
                market_question=request.market_question,
                market_description=request.market_description,
                condition_id=request.condition_id,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date,
                trader_stats_json=request.trader_stats_json or "{}",
                smart_money_json=request.smart_money_json or "{}",
            )

            metadata = {"backend": "llm-chain"}
            metadata.update(provider_meta)

            return analysis_pb2.GenerateNewsResponse(
                success=True,
                headline=article.get("headline", ""),
                summary=article.get("summary", ""),
                body=article.get("body", ""),
                sentiment=article.get("sentiment", "neutral"),
                confidence=float(article.get("confidence", 0.5)),
                key_insights=list(article.get("key_insights", [])),
                trader_behavior_summary=article.get("trader_behavior_summary", ""),
                market_outlook=article.get("market_outlook", ""),
                tags=list(article.get("tags", [])),
                market_question=article.get("market_question", request.market_question),
                condition_id=article.get("condition_id", request.condition_id),
                generated_at=article.get("generated_at", datetime.utcnow().isoformat()),
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"GenerateNews error: {e}")
            return analysis_pb2.GenerateNewsResponse(
                success=False,
                error=str(e),
                generated_at=datetime.utcnow().isoformat(),
            )

    async def GenerateNewsBatch(self, request, context):
        """Generate news articles for multiple markets."""
        try:
            markets = list(request.markets)
            logger.info(f"GenerateNewsBatch request for {len(markets)} markets")

            articles = []
            for mkt in markets:
                try:
                    resp = await self.GenerateNews(mkt, context)
                    articles.append(resp)
                except Exception as e:
                    logger.error(f"Batch news error for {mkt.market_question}: {e}")
                    articles.append(
                        analysis_pb2.GenerateNewsResponse(
                            success=False,
                            error=str(e),
                            market_question=mkt.market_question,
                            condition_id=mkt.condition_id,
                            generated_at=datetime.utcnow().isoformat(),
                        )
                    )

            return analysis_pb2.GenerateNewsBatchResponse(
                success=True,
                articles=articles,
            )
        except Exception as e:
            logger.error(f"GenerateNewsBatch error: {e}")
            return analysis_pb2.GenerateNewsBatchResponse(
                success=False,
                error=str(e),
            )


async def serve():
    """Start the gRPC server."""
    settings = get_settings()

    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    analysis_pb2_grpc.add_AnalysisServiceServicer_to_server(AnalysisServicer(), server)

    # Enable reflection for debugging
    SERVICE_NAMES = (
        analysis_pb2.DESCRIPTOR.services_by_name["AnalysisService"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(SERVICE_NAMES, server)

    listen_addr = f"0.0.0.0:{settings.grpc_port}"
    server.add_insecure_port(listen_addr)

    logger.info(f"Starting {settings.service_name} on {listen_addr}")

    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
