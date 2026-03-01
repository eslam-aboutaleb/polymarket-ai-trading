"""AI Analysis API routes for trading recommendations."""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime
from sqlalchemy.orm import Session
import json
import asyncio
import logging
import hashlib

from app.api.routes.auth import get_current_user_from_token
from app.config import get_settings, AIBackend
from app.grpc_clients.analysis_client import AnalysisClient
from app.models.user_settings import UserSettings, AIBackendType
from app.utils.database import get_db
from app.utils.cache import opportunity_cache
from app.utils.time import utc_now
from app.services.leaderboard_service import fetch_trader_trades
from app.services.polymarket_service import get_polymarket_service

router = APIRouter(prefix="/api/analysis", tags=["analysis"])
settings = get_settings()
logger = logging.getLogger(__name__)


# Request/Response schemas
class MarketAnalysisRequest(BaseModel):
    """Request for market analysis."""
    market_title: str = Field(..., description="Title of the prediction market")
    market_description: str = Field(..., description="Full market description")
    yes_price: float = Field(..., ge=0, le=1, description="Current YES token price")
    no_price: float = Field(..., ge=0, le=1, description="Current NO token price")
    volume_24h: float = Field(default=0, description="24-hour trading volume in USD")
    end_date: str = Field(..., description="Market resolution date (ISO format)")
    include_research: bool = Field(default=True, description="Include web research in analysis")


class QuickAnalysisRequest(BaseModel):
    """Request for quick market analysis."""
    question: str = Field(..., description="Market question")
    current_price: float = Field(..., ge=0, le=1, description="Current price")


class QuickGroupSubMarketRequest(BaseModel):
    """Sub-market option for grouped quick analysis."""
    label: str = Field(..., min_length=1, description="Sub-market display label")
    question: Optional[str] = Field(default=None, description="Full sub-market question")
    yes_price: float = Field(..., ge=0, le=1, description="YES price for this option")
    no_price: float = Field(..., ge=0, le=1, description="NO price for this option")
    volume_24h: float = Field(default=0, ge=0, description="24-hour volume for this option")
    liquidity: float = Field(default=0, ge=0, description="Liquidity for this option")
    condition_id: Optional[str] = Field(default=None, description="Condition ID for this option")


class QuickGroupAnalysisRequest(BaseModel):
    """Request for grouped quick analysis (event with multiple options)."""
    event_title: str = Field(..., min_length=1, description="Event title")
    event_slug: Optional[str] = Field(default=None, description="Event slug")
    event_volume: float = Field(default=0, ge=0, description="Aggregate event volume")
    event_liquidity: float = Field(default=0, ge=0, description="Aggregate event liquidity")
    sub_markets: List[QuickGroupSubMarketRequest] = Field(
        ...,
        min_items=2,
        max_items=20,
        description="Sub-market choices belonging to the same grouped event",
    )


class MarketScanRequest(BaseModel):
    """Request for multi-market scan."""
    markets: List[dict] = Field(..., description="List of markets to scan")


class RiskAssessmentRequest(BaseModel):
    """Request for risk assessment."""
    market_title: str
    position_size: float = Field(..., gt=0, description="Position size in USD")
    entry_price: float = Field(..., ge=0, le=1)
    days_to_expiry: int = Field(..., gt=0)
    correlation_info: Optional[str] = None


class TradePlanRequest(BaseModel):
    """Request for trade execution plan."""
    action: str = Field(..., pattern="^(buy_yes|buy_no|sell_yes|sell_no)$")
    market_title: str
    target_size: float = Field(..., gt=0)
    current_price: float = Field(..., ge=0, le=1)
    order_book: Optional[dict] = None


class TraderAnalysisRequest(BaseModel):
    """Request for trader profile analysis."""
    wallet_address: str = Field(..., description="Trader's wallet address")
    display_name: Optional[str] = None
    total_pnl: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    markets_traded: int = 0
    recent_trades_json: str = "[]"
    # If True, backend will fetch all trades from Polymarket APIs (default)
    fetch_real_trades: bool = True


class CopyTradeEvalRequest(BaseModel):
    """Request for copy-trade evaluation."""
    trader_wallet: str
    trader_stats: str = ""
    market_id: str = ""
    market_title: str = ""
    trade_side: str = Field(default="BUY", pattern="^(BUY|SELL)$")
    trade_size: float = 0.0
    current_price: float = Field(default=0.5, ge=0, le=1)
    user_risk_profile: str = "max_position_daily_loss"


class OpportunityScanRequest(BaseModel):
    """Request for AI opportunity scanning."""
    limit: int = Field(default=40, ge=1, le=80, description="Max markets to scan")
    force_refresh: bool = Field(default=False, description="Bypass cache")


class AnalysisResponse(BaseModel):
    """Standard analysis response."""
    success: bool
    data: dict
    timestamp: str
    backend: Optional[str] = None


async def get_user_backend(user_id: int, db: Session) -> AIBackend:
    """Get user's preferred AI backend from settings."""
    settings_record = db.query(UserSettings).filter(
        UserSettings.user_id == user_id
    ).first()
    
    if settings_record and settings_record.ai_backend == AIBackendType.CLI_AGENT.value:
        return AIBackend.CLI_AGENT
    return AIBackend.LLM_CHAIN


async def get_analysis_client_for_user(user_id: int, db: Session) -> AnalysisClient:
    """Get analysis client configured for user's preferred backend."""
    backend = await get_user_backend(user_id, db)
    return AnalysisClient(backend=backend)


@router.post("/market", response_model=AnalysisResponse)
async def analyze_market(
    request: MarketAnalysisRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Perform comprehensive AI analysis of a Polymarket market.
    Uses the user's preferred AI backend (LLM Chain or CLI Agent).
    Includes web research for current news and events.
    
    Rate limited to 10 requests per minute.
    """
    try:
        user_id = current_user.get("user_id")
        client = await get_analysis_client_for_user(user_id, db)
        
        result = await client.analyze_market(
            market_title=request.market_title,
            market_description=request.market_description,
            yes_price=request.yes_price,
            no_price=request.no_price,
            volume_24h=request.volume_24h,
            end_date=request.end_date,
            include_research=request.include_research
        )
        
        await client.close()
        
        # TODO: Store assessment in database for history tracking
        
        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Analysis failed: {str(e)}"
        )


@router.post("/market/stream")
async def analyze_market_stream(
    request: MarketAnalysisRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Stream market analysis via Server-Sent Events (SSE).
    Opens immediately and delivers chunks as the LLM generates them.
    """
    user_id = current_user.get("user_id")
    client = await get_analysis_client_for_user(user_id, db)

    async def event_generator():
        try:
            async for chunk in client.analyze_market_stream(
                market_title=request.market_title,
                market_description=request.market_description,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date,
                include_research=request.include_research
            ):
                payload = json.dumps({"chunk": chunk})
                yield f"data: {payload}\n\n"
            # Send final event
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.error(f"Streaming analysis error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await client.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/quick", response_model=AnalysisResponse)
async def quick_analysis(
    request: QuickAnalysisRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Quick 2-3 sentence market analysis without web research.
    Faster but less comprehensive than full analysis.
    """
    try:
        user_id = current_user.get("user_id")
        client = await get_analysis_client_for_user(user_id, db)
        
        result = await client.quick_analysis(
            question=request.question,
            current_price=request.current_price
        )
        
        await client.close()
        
        return AnalysisResponse(
            success=True,
            data={"analysis": result.get("analysis", ""), "question": request.question},
            timestamp=utc_now().isoformat(),
            backend=client.backend.value
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Quick analysis failed: {str(e)}"
        )


@router.post("/quick-group", response_model=AnalysisResponse)
async def quick_group_analysis(
    request: QuickGroupAnalysisRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Quick grouped-event analysis without web research.
    Treats one event with multiple options as a single trade decision.
    """
    try:
        user_id = current_user.get("user_id")
        client = await get_analysis_client_for_user(user_id, db)

        result = await client.quick_group_analysis(
            event_title=request.event_title,
            event_slug=request.event_slug or "",
            event_volume=request.event_volume,
            event_liquidity=request.event_liquidity,
            sub_markets=[
                {
                    "label": sm.label,
                    "question": sm.question or sm.label,
                    "yes_price": sm.yes_price,
                    "no_price": sm.no_price,
                    "volume_24h": sm.volume_24h,
                    "liquidity": sm.liquidity,
                    "condition_id": sm.condition_id or "",
                }
                for sm in request.sub_markets
            ],
        )

        await client.close()

        return AnalysisResponse(
            success=True,
            data={
                "analysis": result.get("analysis", ""),
                "event_title": request.event_title,
                "recommended_option": result.get("recommended_option", ""),
                "recommended_side": result.get("recommended_side", ""),
            },
            timestamp=utc_now().isoformat(),
            backend=client.backend.value
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Quick group analysis failed: {str(e)}"
        )


@router.post("/scan", response_model=AnalysisResponse)
async def scan_markets(
    request: MarketScanRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Scan multiple markets to identify top opportunities.
    Returns ranked list of markets with highest expected value.
    """
    if len(request.markets) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maximum 20 markets per scan"
        )
    
    try:
        user_id = current_user.get("user_id")
        client = await get_analysis_client_for_user(user_id, db)
        
        result = await client.scan_markets(markets=request.markets)
        
        await client.close()
        
        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Market scan failed: {str(e)}"
        )


@router.post("/risk", response_model=AnalysisResponse)
async def assess_risk(
    request: RiskAssessmentRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Assess risk profile for a potential trade.
    Returns risk rating, maximum loss scenarios, and recommendations.
    """
    try:
        user_id = current_user.get("user_id")
        client = await get_analysis_client_for_user(user_id, db)
        
        result = await client.assess_risk(
            market_title=request.market_title,
            position_size=request.position_size,
            entry_price=request.entry_price,
            days_to_expiry=request.days_to_expiry,
            correlation_info=request.correlation_info or "No correlation data"
        )
        
        await client.close()
        
        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Risk assessment failed: {str(e)}"
        )


@router.post("/trade-plan", response_model=AnalysisResponse)
async def generate_trade_plan(
    request: TradePlanRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Generate a trade execution plan with order type recommendations,
    price levels, and risk management stops.
    """
    try:
        user_id = current_user.get("user_id")
        client = await get_analysis_client_for_user(user_id, db)
        
        result = await client.generate_trade_plan(
            action=request.action,
            market_title=request.market_title,
            target_size=request.target_size,
            current_price=request.current_price,
            order_book=request.order_book
        )
        
        await client.close()
        
        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Trade plan generation failed: {str(e)}"
        )


# ────────────── Trader Analysis & Copy-Trade Evaluation ──────────────

async def _enrich_trader_request(request: TraderAnalysisRequest) -> dict:
    """
    Fetch real trade data from Polymarket APIs and build enriched params
    for the AI analysis. Returns dict with all the fields the AI needs.
    """
    enriched = {
        "wallet_address": request.wallet_address,
        "display_name": request.display_name or "",
        "total_pnl": request.total_pnl,
        "win_rate": request.win_rate,
        "trade_count": request.trade_count,
        "markets_traded": request.markets_traded,
        "recent_trades_json": request.recent_trades_json,
    }

    if not request.fetch_real_trades:
        return enriched

    try:
        logger.info("Fetching real trades for trader %s", request.wallet_address[:10])
        trade_data = await fetch_trader_trades(request.wallet_address, max_trades=500)
        stats = trade_data.get("stats", {})
        trade_summary = trade_data.get("trade_summary", "")

        # Override with real data
        if stats.get("win_rate", 0) > 0 or stats.get("total_trades", 0) > 0:
            enriched["win_rate"] = stats["win_rate"]
        if stats.get("total_trades", 0) > 0:
            enriched["trade_count"] = stats["total_trades"]
        if stats.get("unique_markets", 0) > 0:
            enriched["markets_traded"] = stats["unique_markets"]
        if stats.get("total_volume", 0) > 0 and request.total_pnl == 0:
            enriched["total_pnl"] = stats.get("total_pnl", request.total_pnl)

        # Replace the trades JSON with comprehensive trade summary
        enriched["recent_trades_json"] = trade_summary

        logger.info(
            "Enriched trader data: %d trades, %.1f%% win rate, %d markets",
            enriched["trade_count"],
            enriched["win_rate"],
            enriched["markets_traded"],
        )
    except Exception as e:
        logger.warning("Failed to fetch real trades for %s: %s", request.wallet_address[:10], e)
        # Fall back to frontend-supplied data

    return enriched


@router.post("/trader/stream")
async def analyze_trader_stream(
    request: TraderAnalysisRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Stream AI trader analysis via Server-Sent Events (SSE).
    First fetches ALL real trades from Polymarket APIs, then analyzes
    trading patterns, risk profile, and copy-worthiness.
    """
    user_id = current_user.get("user_id")

    # Fetch real trade data BEFORE starting the stream
    enriched = await _enrich_trader_request(request)

    client = await get_analysis_client_for_user(user_id, db)

    async def event_generator():
        try:
            # Send a status update while AI processes
            yield f"data: {json.dumps({'chunk': ''})}\n\n"

            async for chunk in client.analyze_trader_stream(
                wallet_address=enriched["wallet_address"],
                display_name=enriched["display_name"],
                total_pnl=enriched["total_pnl"],
                win_rate=enriched["win_rate"],
                trade_count=enriched["trade_count"],
                markets_traded=enriched["markets_traded"],
                recent_trades_json=enriched["recent_trades_json"],
                user_id=str(user_id),
            ):
                payload = json.dumps({"chunk": chunk})
                yield f"data: {payload}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.error(f"Streaming trader analysis error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await client.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/trader", response_model=AnalysisResponse)
async def analyze_trader(
    request: TraderAnalysisRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Full AI analysis of a Polymarket trader profile.
    Fetches real trades from Polymarket APIs, then performs
    internet research, pattern analysis, and copy-worthiness rating (1-10).
    """
    try:
        user_id = current_user.get("user_id")
        enriched = await _enrich_trader_request(request)
        client = await get_analysis_client_for_user(user_id, db)

        result = await client.analyze_trader(
            wallet_address=enriched["wallet_address"],
            display_name=enriched["display_name"],
            total_pnl=enriched["total_pnl"],
            win_rate=enriched["win_rate"],
            trade_count=enriched["trade_count"],
            markets_traded=enriched["markets_traded"],
            recent_trades_json=enriched["recent_trades_json"],
            user_id=str(user_id),
        )

        await client.close()

        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Trader analysis failed: {str(e)}",
        )


@router.post("/copy-trade-eval", response_model=AnalysisResponse)
async def evaluate_copy_trade(
    request: CopyTradeEvalRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Evaluate whether to copy a specific trade.
    Returns recommendation (copy/reduce_size/avoid), confidence, and analysis.
    """
    try:
        user_id = current_user.get("user_id")
        client = await get_analysis_client_for_user(user_id, db)

        result = await client.evaluate_copy_trade(
            trader_wallet=request.trader_wallet,
            trader_stats=request.trader_stats,
            market_id=request.market_id,
            market_title=request.market_title,
            trade_side=request.trade_side,
            trade_size=request.trade_size,
            current_price=request.current_price,
            user_risk_profile=request.user_risk_profile,
            user_id=str(user_id),
        )

        await client.close()

        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Copy trade evaluation failed: {str(e)}",
        )


@router.get("/health")
async def health_check():
    """Check if the analysis services are healthy."""
    from app.grpc_clients.analysis_client import AnalysisClient
    
    # Check both AI backends
    llm_client = AnalysisClient(backend=AIBackend.LLM_CHAIN)
    llm_healthy = await llm_client.health_check()
    await llm_client.close()
    
    cli_client = AnalysisClient(backend=AIBackend.CLI_AGENT)
    cli_healthy = await cli_client.health_check()
    await cli_client.close()
    
    return {
        "status": "healthy" if (llm_healthy or cli_healthy) else "unhealthy",
        "backends": {
            "llm_chain": {
                "healthy": llm_healthy,
                "host": f"{settings.llm_chain_grpc_host}:{settings.llm_chain_grpc_port}"
            },
            "cli_agent": {
                "healthy": cli_healthy,
                "host": f"{settings.cli_agent_grpc_host}:{settings.cli_agent_grpc_port}"
            }
        }
    }


# ────────────── Opportunity Scanning ─────────────────────────────

OPPORTUNITY_CACHE_TTL = 1200  # 20 minutes


@router.post("/opportunities/stream")
async def stream_opportunity_scan(
    request: OpportunityScanRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    AI-powered opportunity scanning via SSE.

    1. Fetches high-PNL-potential markets (yes AND no < 90¢)
    2. For each market, gathers smart money / whale positioning data
    3. Sends each market to the AI for credibility validation + scoring
    4. Streams per-market results as SSE events so the frontend can
       progressively display and sort cards

    Results are cached for 20 minutes unless force_refresh is True.

    SSE event format per market:
      data: {"market_id": "...", "scores": {...}, "done_count": N, "total": M}
    Final:
      data: {"all_done": true, "results": [...]}
    """
    user_id = current_user.get("user_id")
    service = get_polymarket_service()

    async def event_generator():
        # ── Check cache ──────────────────────────────────────
        cache_key = f"opportunities:scan:{user_id}:{request.limit}"
        if not request.force_refresh:
            cached = opportunity_cache.get(cache_key)
            if cached:
                logger.info("Serving opportunity scan from cache for user %s", user_id)
                # Emit all cached results at once
                for i, r in enumerate(cached):
                    payload = json.dumps({
                        "market_id": r.get("condition_id", ""),
                        "scores": r,
                        "done_count": i + 1,
                        "total": len(cached),
                        "cached": True,
                    })
                    yield f"data: {payload}\n\n"
                yield f"data: {json.dumps({'all_done': True, 'total': len(cached), 'cached': True})}\n\n"
                return

        # ── Fetch high-PNL markets ───────────────────────────
        try:
            yield f"data: {json.dumps({'status': 'fetching_markets'})}\n\n"
            markets = await service.get_high_pnl_markets(limit=request.limit)
            if not markets:
                yield f"data: {json.dumps({'error': 'No high-PNL markets found'})}\n\n"
                return
            yield f"data: {json.dumps({'status': 'markets_loaded', 'total': len(markets)})}\n\n"
        except Exception as e:
            logger.error("Failed to fetch high-PNL markets: %s", e)
            yield f"data: {json.dumps({'error': f'Failed to fetch markets: {str(e)}'})}\n\n"
            return

        # ── Create analysis client ───────────────────────────
        client = await get_analysis_client_for_user(user_id, db)
        all_results = []

        try:
            # ── Group markets by _event_slug ──────────────────
            from collections import OrderedDict
            groups: OrderedDict[str, list] = OrderedDict()
            for market in markets:
                slug = market.get("_event_slug") or market.get("question", "")
                groups.setdefault(slug, []).append(market)

            # Compute total individual markets for progress
            total = len(markets)
            done_count = 0

            for slug, group_markets in groups.items():
                if len(group_markets) > 1:
                    # ── EVENT GROUP: one AI call for the whole group ──
                    event_title = slug.replace("-", " ").title()
                    # Use event-level volume/liquidity from first market
                    first = group_markets[0]
                    event_volume = str(first.get("_event_volume", "0") or "0")
                    event_liquidity = str(first.get("_event_liquidity", "0") or "0")

                    # Build sub_markets list for the AI
                    sub_markets_for_ai = []
                    condition_ids = []
                    for m in group_markets:
                        cid = m.get("condition_id") or m.get("conditionId") or m.get("id", "")
                        condition_ids.append(cid)
                        prices = m.get("outcomePrices", [])
                        try:
                            yes_p = float(prices[0]) if prices else 0.5
                            no_p = float(prices[1]) if len(prices) > 1 else 1.0 - yes_p
                        except (ValueError, TypeError):
                            yes_p, no_p = 0.5, 0.5
                        sub_markets_for_ai.append({
                            "label": m.get("groupItemTitle") or m.get("question", "Unknown"),
                            "yes_price": yes_p,
                            "no_price": no_p,
                            "volume_24h": float(m.get("volume24hr", 0) or 0),
                            "liquidity": float(m.get("liquidity", 0) or 0),
                        })

                    # Gather smart money for the first market as representative
                    smart_money_context = "Smart money data unavailable"
                    smart_money_raw = {}
                    rep_cid = condition_ids[0] if condition_ids[0] else ""
                    if rep_cid:
                        try:
                            smart_money_raw = await service.get_smart_money_analysis(rep_cid)
                            smart_money_context = smart_money_raw.get("context_text", smart_money_context)
                        except Exception as sme:
                            logger.warning("Smart money fetch failed for event %s: %s", slug[:20], sme)

                    # One AI call for entire event group
                    try:
                        event_scores = await client.scan_event_opportunity(
                            event_title=event_title,
                            sub_markets=sub_markets_for_ai,
                            event_volume=event_volume,
                            event_liquidity=event_liquidity,
                            smart_money_context=smart_money_context,
                        )
                    except Exception as ae:
                        logger.error("AI event scoring failed for %s: %s", event_title[:40], ae)
                        event_scores = {
                            "event_title": event_title,
                            "ai_score": 0,
                            "risk_level": "high",
                            "pnl_potential": 0,
                            "credibility_score": 0,
                            "smart_money_signal": "neutral",
                            "smart_money_summary": "Analysis failed",
                            "recommendation": "hold",
                            "recommended_side": "YES",
                            "recommended_option": "",
                            "reasoning": f"AI analysis error: {str(ae)[:100]}",
                            "search_summary": "",
                            "key_risks": ["Analysis failed"],
                        }

                    # Emit shared scores for each sub-market in the group
                    for m, cid in zip(group_markets, condition_ids):
                        scores = {
                            "condition_id": cid,
                            "market_title": m.get("question", "Unknown"),
                            "ai_score": event_scores.get("ai_score", 50),
                            "risk_level": event_scores.get("risk_level", "medium"),
                            "pnl_potential": event_scores.get("pnl_potential", 0),
                            "credibility_score": event_scores.get("credibility_score", 50),
                            "smart_money_signal": event_scores.get("smart_money_signal", "neutral"),
                            "smart_money_summary": event_scores.get("smart_money_summary", ""),
                            "recommendation": event_scores.get("recommendation", "hold"),
                            "recommended_side": event_scores.get("recommended_side", "YES"),
                            "recommended_option": event_scores.get("recommended_option", ""),
                            "reasoning": event_scores.get("reasoning", ""),
                            "search_summary": event_scores.get("search_summary", ""),
                            "key_risks": event_scores.get("key_risks", []),
                            "whale_count": smart_money_raw.get("whale_count", 0),
                            "whale_bias": smart_money_raw.get("whale_bias", "MIXED"),
                            "yes_whale_pct": smart_money_raw.get("yes_whale_pct", 50),
                            "no_whale_pct": smart_money_raw.get("no_whale_pct", 50),
                        }
                        all_results.append(scores)
                        done_count += 1
                        payload = json.dumps({
                            "market_id": cid,
                            "scores": scores,
                            "done_count": done_count,
                            "total": total,
                        })
                        yield f"data: {payload}\n\n"

                else:
                    # ── SINGLE MARKET: existing per-market flow ───
                    market = group_markets[0]
                    market_title = market.get("question", "Unknown")
                    condition_id = (
                        market.get("condition_id")
                        or market.get("conditionId")
                        or market.get("id", "")
                    )
                    prices = market.get("outcomePrices", [])
                    try:
                        yes_price = float(prices[0]) if prices else 0.5
                        no_price = float(prices[1]) if len(prices) > 1 else 1.0 - yes_price
                    except (ValueError, TypeError):
                        yes_price, no_price = 0.5, 0.5

                    pnl_potential = market.get("pnl_potential", 0)
                    volume_24h = float(market.get("volume24hr", 0) or 0)
                    liquidity = float(market.get("liquidity", 0) or 0)
                    end_date = market.get("endDate") or market.get("end_date_iso") or ""

                    # Fetch smart money
                    smart_money_context = "Smart money data unavailable"
                    smart_money_raw = {}
                    if condition_id:
                        try:
                            smart_money_raw = await service.get_smart_money_analysis(condition_id)
                            smart_money_context = smart_money_raw.get("context_text", smart_money_context)
                        except Exception as sme:
                            logger.warning("Smart money fetch failed for %s: %s", condition_id[:16], sme)

                    # AI scoring
                    try:
                        scores = await client.scan_opportunity(
                            market_title=market_title,
                            market_description=market.get("description", market_title),
                            yes_price=yes_price,
                            no_price=no_price,
                            volume_24h=volume_24h,
                            end_date=end_date,
                            pnl_potential=pnl_potential,
                            smart_money_context=smart_money_context,
                            liquidity=liquidity,
                            condition_id=condition_id,
                        )
                    except Exception as ae:
                        logger.error("AI scoring failed for %s: %s", market_title[:40], ae)
                        scores = {
                            "condition_id": condition_id,
                            "market_title": market_title,
                            "ai_score": 0,
                            "risk_level": "high",
                            "pnl_potential": pnl_potential,
                            "credibility_score": 0,
                            "smart_money_signal": "neutral",
                            "smart_money_summary": "Analysis failed",
                            "recommendation": "hold",
                            "recommended_side": "YES",
                            "reasoning": f"AI analysis error: {str(ae)[:100]}",
                            "search_summary": "",
                            "key_risks": ["Analysis failed"],
                        }

                    scores["whale_count"] = smart_money_raw.get("whale_count", 0)
                    scores["whale_bias"] = smart_money_raw.get("whale_bias", "MIXED")
                    scores["yes_whale_pct"] = smart_money_raw.get("yes_whale_pct", 50)
                    scores["no_whale_pct"] = smart_money_raw.get("no_whale_pct", 50)

                    all_results.append(scores)
                    done_count += 1
                    payload = json.dumps({
                        "market_id": condition_id,
                        "scores": scores,
                        "done_count": done_count,
                        "total": total,
                    })
                    yield f"data: {payload}\n\n"

            # ── Cache results ─────────────────────────────────
            opportunity_cache.set(cache_key, all_results, ttl_seconds=OPPORTUNITY_CACHE_TTL)

            yield f"data: {json.dumps({'all_done': True, 'total': len(all_results)})}\n\n"

        except Exception as e:
            logger.error("Opportunity scan stream error: %s", e, exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await client.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
