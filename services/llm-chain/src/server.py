"""
gRPC server for LLM Chain Analysis Service.
"""
import asyncio
import json
import logging
from concurrent import futures
from datetime import datetime

import grpc
from grpc_reflection.v1alpha import reflection

from src.config import get_settings
from src.analysis_chain import get_trading_chain

# Import generated protobuf modules (will be generated from proto file)
import analysis_pb2
import analysis_pb2_grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AnalysisServicer(analysis_pb2_grpc.AnalysisServiceServicer):
    """Implementation of AnalysisService for LLM Chain."""
    
    def __init__(self):
        self.settings = get_settings()
        self.chain = get_trading_chain()
        logger.info(f"Initialized {self.settings.service_name} v{self.settings.service_version}")
    
    async def AnalyzeMarket(self, request, context):
        """Comprehensive market analysis with optional research."""
        try:
            logger.info(f"AnalyzeMarket request for: {request.market_title}")
            
            result = await self.chain.analyze_market(
                market_title=request.market_title,
                market_description=request.market_description,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date,
                include_research=request.include_research
            )
            
            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                research_context=result.get("research_context", ""),
                timestamp=result["timestamp"],
                metadata={"backend": "llm-chain", "model": self.settings.llm_model}
            )
        except Exception as e:
            logger.error(f"AnalyzeMarket error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat()
            )
    
    async def QuickAnalysis(self, request, context):
        """Quick 2-3 sentence analysis."""
        try:
            logger.info(f"QuickAnalysis request: {request.question}")
            
            result = await self.chain.quick_analysis(
                question=request.question,
                current_price=request.current_price
            )
            
            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result,
                timestamp=datetime.utcnow().isoformat(),
                metadata={"backend": "llm-chain"}
            )
        except Exception as e:
            logger.error(f"QuickAnalysis error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat()
            )
    
    async def ScanMarkets(self, request, context):
        """Scan multiple markets for opportunities."""
        try:
            markets = [
                {
                    "title": m.title,
                    "description": m.description,
                    "yes_price": m.yes_price,
                    "no_price": m.no_price,
                    "volume_24h": m.volume_24h,
                    "end_date": m.end_date,
                    "market_id": m.market_id
                }
                for m in request.markets
            ]
            
            logger.info(f"ScanMarkets request for {len(markets)} markets")
            
            result = await self.chain.scan_markets(markets)
            
            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                timestamp=result["timestamp"],
                metadata={"backend": "llm-chain", "markets_scanned": str(result["markets_scanned"])}
            )
        except Exception as e:
            logger.error(f"ScanMarkets error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat()
            )
    
    async def AssessRisk(self, request, context):
        """Assess risk for a potential trade."""
        try:
            logger.info(f"AssessRisk request for: {request.market_title}")
            
            result = await self.chain.assess_risk(
                market_title=request.market_title,
                position_size=request.position_size,
                entry_price=request.entry_price,
                days_to_expiry=request.days_to_expiry,
                correlation_info=request.correlation_info or "No correlation data"
            )
            
            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                timestamp=result["timestamp"],
                metadata={"backend": "llm-chain"}
            )
        except Exception as e:
            logger.error(f"AssessRisk error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat()
            )
    
    async def GenerateTradePlan(self, request, context):
        """Generate trade execution plan."""
        try:
            logger.info(f"GenerateTradePlan request: {request.action} on {request.market_title}")
            
            order_book = None
            if request.order_book_json:
                try:
                    order_book = json.loads(request.order_book_json)
                except json.JSONDecodeError:
                    pass
            
            result = await self.chain.generate_trade_plan(
                action=request.action,
                market_title=request.market_title,
                target_size=request.target_size,
                current_price=request.current_price,
                order_book=order_book
            )
            
            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                timestamp=result["timestamp"],
                metadata={"backend": "llm-chain"}
            )
        except Exception as e:
            logger.error(f"GenerateTradePlan error: {e}")
            return analysis_pb2.AnalysisResponse(
                success=False,
                error=str(e),
                timestamp=datetime.utcnow().isoformat()
            )
    
    async def AnalyzeMarketStream(self, request, context):
        """Stream market analysis for real-time updates."""
        try:
            logger.info(f"AnalyzeMarketStream request for: {request.market_title}")
            
            async for chunk in self.chain.analyze_market_stream(
                market_title=request.market_title,
                market_description=request.market_description,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date,
                include_research=request.include_research
            ):
                yield analysis_pb2.AnalysisChunk(
                    chunk=chunk["chunk"],
                    is_final=chunk["is_final"],
                    chunk_type=chunk["chunk_type"]
                )
        except Exception as e:
            logger.error(f"AnalyzeMarketStream error: {e}")
            yield analysis_pb2.AnalysisChunk(
                chunk=f"Error: {str(e)}",
                is_final=True,
                chunk_type="error"
            )
    
    async def AnalyzeTrader(self, request, context):
        """Full trader profile analysis with internet research."""
        try:
            logger.info(f"AnalyzeTrader request for wallet: {request.wallet_address}")

            result = await self.chain.analyze_trader(
                wallet_address=request.wallet_address,
                display_name=request.display_name,
                total_pnl=request.total_pnl,
                win_rate=request.win_rate,
                trade_count=request.trade_count,
                markets_traded=request.markets_traded,
                recent_trades_json=request.recent_trades_json,
            )

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                research_context=result.get("research_context", ""),
                timestamp=result["timestamp"],
                metadata={"backend": "llm-chain", "type": "trader_analysis"},
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
            logger.info(f"EvaluateCopyTrade for trader {request.trader_wallet} on {request.market_title}")

            result = await self.chain.evaluate_copy_trade(
                trader_wallet=request.trader_wallet,
                trader_stats=request.trader_stats,
                market_id=request.market_id,
                market_title=request.market_title,
                trade_side=request.trade_side,
                trade_size=request.trade_size,
                current_price=request.current_price,
                user_risk_profile=request.user_risk_profile,
            )

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result["analysis"],
                research_context=result.get("research_context", ""),
                timestamp=result["timestamp"],
                metadata={
                    "backend": "llm-chain",
                    "type": "copy_trade_eval",
                    "recommendation": result.get("recommendation", ""),
                    "confidence": str(result.get("confidence", 0)),
                    "risk_level": result.get("risk_level", ""),
                },
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
            result = await self.chain.evaluate_inverse_position(
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

            return analysis_pb2.AnalysisResponse(
                success=True,
                analysis=result.get("analysis", ""),
                research_context=result.get("web_summary", ""),
                timestamp=result.get("timestamp", datetime.utcnow().isoformat()),
                metadata={
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
                },
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
                "model": self.settings.llm_model,
            }
        )


async def serve():
    """Start the gRPC server."""
    settings = get_settings()
    
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    analysis_pb2_grpc.add_AnalysisServiceServicer_to_server(AnalysisServicer(), server)
    
    # Enable reflection for debugging
    SERVICE_NAMES = (
        analysis_pb2.DESCRIPTOR.services_by_name['AnalysisService'].full_name,
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
