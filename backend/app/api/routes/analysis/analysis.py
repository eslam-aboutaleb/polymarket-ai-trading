"""AI Analysis API routes for trading recommendations."""

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import optional_plan_quota
from app.api.routes.auth.auth import get_current_user_from_token, get_optional_user_from_token
from app.config import AIBackend, get_settings
from app.grpc_clients.analysis_client import AnalysisClient
from app.models.users.user_settings import AIBackendType
from app.security.rate_limit import limiter
from app.services.analytics.leaderboard_service import fetch_trader_trades
from app.services.analytics.research_quality import (
    BatchCredibility,
    GroupEvidence,
    score_batch_credibility,
)
from app.services.intelligence.forecast_recorder import record_verdict
from app.services.intelligence.llm_gateway import get_llm_gateway
from app.services.intelligence.market_context import (
    MarketContext,
    MarketIdentityError,
    fetch_market_context,
)
from app.services.intelligence.market_research import (
    finalize_research,
    missing_input_research,
    research_prompt,
)
from app.services.intelligence.user_preferences import get_user_settings
from app.services.market_data.market_observations import append_observation, observation_history
from app.services.market_data.polymarket_service import get_polymarket_service
from app.services.users.entitlement_service import CAP_OPPORTUNITY_SCAN
from app.utils.cache import opportunity_cache
from app.utils.database import get_db
from app.utils.time import utc_now

# Semaphore to bound concurrent AI calls during opportunity scanning.
# Allows up to 4 event groups to be analysed simultaneously while
# keeping upstream load manageable.
_SCAN_CONCURRENCY = 4

router = APIRouter(prefix="/api/analysis", tags=["analysis"])
settings = get_settings()
logger = logging.getLogger(__name__)


# Every analysis route spends an LLM call (gRPC to llm-chain / cli-agent),
# and the public ones accept an anonymous caller, so a single client could
# otherwise consume the whole global 100/minute budget on them — starving
# the authenticated routes that share it. The budget rides on a stub
# dependency (the auth.py pattern): it takes the starlette Request, so the
# limiter keys off the client IP without touching the handler signatures.
@limiter.limit("10/minute")
def _analysis_rate_limit(request: Request) -> None:
    """Rate-limit the public analysis routes: 10/minute per IP."""


@limiter.limit("20/minute")
def _analysis_scan_rate_limit(request: Request) -> None:
    """Rate-limit the bulk opportunity scans: 20/minute per IP.

    Higher than a single analysis because one call already fans out over
    every market the caller asked for — the point is to bound how often
    that fan-out starts, not each of its internal calls.
    """


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


class MarketResearchRequest(BaseModel):
    """Canonical identity only: callers cannot supply replacement prices/rules."""

    model_config = {"extra": "forbid"}
    condition_id: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    include_research: bool = True


class QuickAnalysisRequest(BaseModel):
    """Request for quick market analysis."""

    question: str = Field(..., description="Market question")
    current_price: float = Field(..., ge=0, le=1, description="Current price")


class QuickGroupSubMarketRequest(BaseModel):
    """Sub-market option for grouped quick analysis."""

    label: str = Field(..., min_length=1, description="Sub-market display label")
    question: str | None = Field(default=None, description="Full sub-market question")
    yes_price: float = Field(..., ge=0, le=1, description="YES price for this option")
    no_price: float = Field(..., ge=0, le=1, description="NO price for this option")
    volume_24h: float = Field(default=0, ge=0, description="24-hour volume for this option")
    liquidity: float = Field(default=0, ge=0, description="Liquidity for this option")
    condition_id: str | None = Field(default=None, description="Condition ID for this option")


class QuickGroupAnalysisRequest(BaseModel):
    """Request for grouped quick analysis (event with multiple options)."""

    event_title: str = Field(..., min_length=1, description="Event title")
    event_slug: str | None = Field(default=None, description="Event slug")
    event_volume: float = Field(default=0, ge=0, description="Aggregate event volume")
    event_liquidity: float = Field(default=0, ge=0, description="Aggregate event liquidity")
    sub_markets: list[QuickGroupSubMarketRequest] = Field(
        ...,
        min_length=2,
        max_length=20,
        description="Sub-market choices belonging to the same grouped event",
    )


class MarketScanRequest(BaseModel):
    """Request for multi-market scan."""

    markets: list[dict] = Field(..., description="List of markets to scan")


class RiskAssessmentRequest(BaseModel):
    """Request for risk assessment."""

    market_title: str
    position_size: float = Field(..., gt=0, description="Position size in USD")
    entry_price: float = Field(..., ge=0, le=1)
    days_to_expiry: int = Field(..., gt=0)
    correlation_info: str | None = None


class TradePlanRequest(BaseModel):
    """Request for trade execution plan."""

    action: str = Field(..., pattern="^(buy_yes|buy_no|sell_yes|sell_no)$")
    market_title: str
    target_size: float = Field(..., gt=0)
    current_price: float = Field(..., ge=0, le=1)
    order_book: dict | None = None


class TraderAnalysisRequest(BaseModel):
    """Request for trader profile analysis."""

    wallet_address: str = Field(..., description="Trader's wallet address")
    display_name: str | None = None
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


class AnalyzeMarketsRequest(BaseModel):
    """Request to analyze a specific set of markets (e.g. from infinite scroll)."""

    markets: list[dict] = Field(..., description="Market objects to analyze")
    force_refresh: bool = Field(default=False, description="Bypass cache")


class AnalysisResponse(BaseModel):
    """Standard analysis response."""

    success: bool
    data: dict
    timestamp: str
    backend: str | None = None


async def get_user_backend(user_id: int | None, db: Session) -> AIBackend:
    """Get user's preferred AI backend from settings."""
    if user_id is None:
        return AIBackend.LLM_CHAIN
    settings_record = get_user_settings(db, user_id)

    if settings_record and settings_record.ai_backend == AIBackendType.CLI_AGENT.value:
        return AIBackend.CLI_AGENT
    return AIBackend.LLM_CHAIN


async def get_analysis_client_for_user(
    user_id: int | None, db: Session
) -> tuple[AnalysisClient, dict[str, Any] | None]:
    """Get the analysis client and LLM config for a user.

    Provider selection follows the gateway precedence: request
    override (not supplied by these routes) → user preference →
    system default. The returned ``llm_config`` is ``None`` when
    no override applies.
    """
    settings_record = None
    if user_id is not None:
        settings_record = get_user_settings(db, user_id)

    gateway = get_llm_gateway()
    backend, llm_config = gateway.resolve_provider(user_settings=settings_record)
    client = AnalysisClient(backend=backend)
    config = llm_config.to_proto_dict() if llm_config else None
    return client, config


@router.get(
    "/markets/{condition_id}/context",
    response_model=MarketContext,
    dependencies=[Depends(_analysis_rate_limit)],
)
async def market_context(
    condition_id: str = Path(pattern=r"^0x[0-9a-fA-F]{64}$"),
    db: Session = Depends(get_db),
):
    """Public, read-only venue context. Never reads credentials or places orders."""
    try:
        context = await fetch_market_context(get_polymarket_service(), condition_id)
        append_observation(db, context)
        return context
    except MarketIdentityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        code = 404 if exc.response.status_code == 404 else 503
        raise HTTPException(status_code=code, detail="Market source unavailable") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Market source unavailable") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503, detail="Market observation storage unavailable"
        ) from exc


class MarketContextHistory(BaseModel):
    observations: list[MarketContext]


@router.get(
    "/markets/{condition_id}/observations",
    response_model=MarketContextHistory,
    dependencies=[Depends(_analysis_rate_limit)],
)
async def market_observations(
    condition_id: str = Path(pattern=r"^0x[0-9a-fA-F]{64}$"),
    as_of: datetime | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Public observations received by as_of, newest first; no private analyses."""
    boundary = as_of or utc_now()
    if boundary.tzinfo is None:
        raise HTTPException(status_code=422, detail="as_of must include a timezone")
    try:
        return MarketContextHistory(
            observations=observation_history(db, condition_id, boundary, limit)
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail="Market observation storage unavailable"
        ) from exc


@router.post("/market/research/stream", dependencies=[Depends(_analysis_rate_limit)])
async def market_research_stream(
    request: MarketResearchRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """Typed research over the existing user-selected agent; no trading actions."""
    context = await market_context(request.condition_id, db=db)

    async def events():
        client = None
        try:
            ready = {"type": "context_ready", "context": context.model_dump(mode="json")}
            yield f"data: {json.dumps(ready)}\n\n"
            prices = {o.outcome: o.indicative_price for o in context.outcomes}
            if context.quote_quality == "unavailable" or not context.rules.text.strip():
                result = missing_input_research(context)
            else:
                user_id = current_user.get("user_id") if current_user else None
                client, llm_config = await get_analysis_client_for_user(user_id, db)
                # Unary generation is labelled as such. No buffered replay is
                # represented as live model tokens, and no artificial pacing.
                yield f"data: {json.dumps({'type': 'research_started', 'transport': 'unary'})}\n\n"
                async with asyncio.timeout(settings.grpc_timeout * 2):
                    response = await client.analyze_market(
                        market_title=context.question,
                        market_description=context.rules.text,
                        research_instructions=research_prompt(context),
                        yes_price=prices["YES"],
                        no_price=prices["NO"],
                        volume_24h=0,
                        end_date=context.rules.market_end_date or "",
                        include_research=request.include_research,
                        llm_config=llm_config,
                    )
                if response.get("metadata", {}).get("research_contract") != "market-research-v1":
                    raise RuntimeError("Provider does not support the typed research contract")
                result = finalize_research(
                    context,
                    response.get("analysis", ""),
                    response.get("research_context", ""),
                    response.get("metadata"),
                )
            payload = {"type": "analysis_completed", "result": result.model_dump(mode="json")}
            yield f"data: {json.dumps(payload)}\n\n"
            yield 'data: {"done":true}\n\n'
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Structured market research failed")
            yield 'data: {"error":"Market research failed. Retry or use legacy analysis."}\n\n'
        finally:
            if client is not None:
                await client.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/market", response_model=AnalysisResponse)
async def analyze_market(
    request: MarketAnalysisRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Perform comprehensive AI analysis of a Polymarket market.
    Uses the user's preferred AI backend (LLM Chain or CLI Agent).
    Includes web research for current news and events.

    Rate limited to 10 requests per minute.
    """
    try:
        user_id = current_user.get("user_id")
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        result = await client.analyze_market(
            market_title=request.market_title,
            market_description=request.market_description,
            yes_price=request.yes_price,
            no_price=request.no_price,
            volume_24h=request.volume_24h,
            end_date=request.end_date,
            include_research=request.include_research,
            llm_config=llm_config,
        )

        await client.close()

        # TODO: Store assessment in database for history tracking

        return AnalysisResponse(
            success=True, data=result, timestamp=utc_now().isoformat(), backend=client.backend.value
        )
    except Exception as e:
        logger.exception("Market analysis failed for %s", request.market_title)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Market analysis failed"
        ) from e


@router.post("/market/stream", dependencies=[Depends(_analysis_rate_limit)])
async def analyze_market_stream(
    request: MarketAnalysisRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Stream market analysis via Server-Sent Events (SSE) (public).
    Opens immediately and delivers chunks as the LLM generates them.
    """
    user_id = current_user.get("user_id") if current_user else None
    client, llm_config = await get_analysis_client_for_user(user_id, db)

    async def event_generator():
        try:
            async for chunk in client.analyze_market_stream(
                market_title=request.market_title,
                market_description=request.market_description,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date,
                include_research=request.include_research,
                llm_config=llm_config,
            ):
                payload = json.dumps({"chunk": chunk})
                yield f"data: {payload}\n\n"
            # Send final event
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.error(f"Streaming analysis error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': 'Market analysis failed'})}\n\n"
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


@router.post(
    "/quick", response_model=AnalysisResponse, dependencies=[Depends(_analysis_rate_limit)]
)
async def quick_analysis(
    request: QuickAnalysisRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Quick 2-3 sentence market analysis without web research (public).
    Faster but less comprehensive than full analysis.
    """
    try:
        user_id = current_user.get("user_id") if current_user else None
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        result = await client.quick_analysis(
            question=request.question,
            current_price=request.current_price,
            llm_config=llm_config,
        )

        await client.close()

        return AnalysisResponse(
            success=True,
            data={"analysis": result.get("analysis", ""), "question": request.question},
            timestamp=utc_now().isoformat(),
            backend=client.backend.value,
        )
    except Exception as e:
        logger.exception("Quick analysis failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Quick analysis failed",
        ) from e


@router.post(
    "/quick-group", response_model=AnalysisResponse, dependencies=[Depends(_analysis_rate_limit)]
)
async def quick_group_analysis(
    request: QuickGroupAnalysisRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Quick grouped-event analysis without web research (public).
    Treats one event with multiple options as a single trade decision.
    """
    try:
        user_id = current_user.get("user_id") if current_user else None
        client, llm_config = await get_analysis_client_for_user(user_id, db)

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
            llm_config=llm_config,
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
            backend=client.backend.value,
        )
    except Exception as e:
        logger.exception("Quick group analysis failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Quick group analysis failed",
        ) from e


@router.post("/scan", response_model=AnalysisResponse, dependencies=[Depends(_analysis_rate_limit)])
async def scan_markets(
    request: MarketScanRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Scan multiple markets to identify top opportunities.
    Returns ranked list of markets with highest expected value.
    """
    if len(request.markets) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Maximum 20 markets per scan"
        )

    try:
        user_id = current_user.get("user_id") if current_user else None
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        result = await client.scan_markets(markets=request.markets, llm_config=llm_config)

        await client.close()

        return AnalysisResponse(
            success=True, data=result, timestamp=utc_now().isoformat(), backend=client.backend.value
        )
    except Exception as e:
        logger.exception("Market scan failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Market scan failed",
        ) from e


@router.post("/risk", response_model=AnalysisResponse)
async def assess_risk(
    request: RiskAssessmentRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Assess risk profile for a potential trade.
    Returns risk rating, maximum loss scenarios, and recommendations.
    """
    try:
        user_id = current_user.get("user_id")
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        result = await client.assess_risk(
            market_title=request.market_title,
            position_size=request.position_size,
            entry_price=request.entry_price,
            days_to_expiry=request.days_to_expiry,
            correlation_info=request.correlation_info or "No correlation data",
            llm_config=llm_config,
        )

        await client.close()

        return AnalysisResponse(
            success=True, data=result, timestamp=utc_now().isoformat(), backend=client.backend.value
        )
    except Exception as e:
        logger.exception("Risk assessment failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Risk assessment failed",
        ) from e


@router.post("/trade-plan", response_model=AnalysisResponse)
async def generate_trade_plan(
    request: TradePlanRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Generate a trade execution plan with order type recommendations,
    price levels, and risk management stops.
    """
    try:
        user_id = current_user.get("user_id")
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        result = await client.generate_trade_plan(
            action=request.action,
            market_title=request.market_title,
            target_size=request.target_size,
            current_price=request.current_price,
            order_book=request.order_book,
            llm_config=llm_config,
        )

        await client.close()

        return AnalysisResponse(
            success=True, data=result, timestamp=utc_now().isoformat(), backend=client.backend.value
        )
    except Exception as e:
        logger.exception("Trade plan generation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Trade plan generation failed",
        ) from e


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


@router.post("/trader/stream", dependencies=[Depends(_analysis_rate_limit)])
async def analyze_trader_stream(
    request: TraderAnalysisRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Stream AI trader analysis via Server-Sent Events (SSE) (public).
    First fetches ALL real trades from Polymarket APIs, then analyzes
    trading patterns, risk profile, and copy-worthiness.
    """
    user_id = current_user.get("user_id") if current_user else None

    # Fetch real trade data BEFORE starting the stream
    enriched = await _enrich_trader_request(request)

    client, llm_config = await get_analysis_client_for_user(user_id, db)

    async def event_generator():
        try:
            # The underlying gRPC AnalyzeTrader method is unary; progress is
            # transport metadata, never a claim of streaming model tokens.
            status_payload = json.dumps(
                {"chunk": "", "stream_kind": "buffered_replay", "phase": "processing"}
            )
            yield f"data: {status_payload}\n\n"

            async for chunk in client.analyze_trader_stream(
                wallet_address=enriched["wallet_address"],
                display_name=enriched["display_name"],
                total_pnl=enriched["total_pnl"],
                win_rate=enriched["win_rate"],
                trade_count=enriched["trade_count"],
                markets_traded=enriched["markets_traded"],
                recent_trades_json=enriched["recent_trades_json"],
                user_id=str(user_id),
                llm_config=llm_config,
            ):
                payload = json.dumps({"chunk": chunk, "stream_kind": "buffered_replay"})
                yield f"data: {payload}\n\n"
            yield f"data: {json.dumps({'done': True, 'stream_kind': 'buffered_replay'})}\n\n"
        except Exception as e:
            logger.error(f"Streaming trader analysis error: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': 'Trader analysis failed'})}\n\n"
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
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        result = await client.analyze_trader(
            wallet_address=enriched["wallet_address"],
            display_name=enriched["display_name"],
            total_pnl=enriched["total_pnl"],
            win_rate=enriched["win_rate"],
            trade_count=enriched["trade_count"],
            markets_traded=enriched["markets_traded"],
            recent_trades_json=enriched["recent_trades_json"],
            user_id=str(user_id),
            llm_config=llm_config,
        )

        await client.close()

        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value,
        )
    except Exception as e:
        logger.exception("Trader analysis failed for %s", request.wallet_address[:10])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Trader analysis failed",
        ) from e


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
        client, llm_config = await get_analysis_client_for_user(user_id, db)

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
            llm_config=llm_config,
        )

        await client.close()

        return AnalysisResponse(
            success=True,
            data=result,
            timestamp=utc_now().isoformat(),
            backend=client.backend.value,
        )
    except Exception as e:
        logger.exception("Copy trade evaluation failed for %s", request.trader_wallet[:10])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Copy trade evaluation failed",
        ) from e


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
                "host": f"{settings.llm_chain_grpc_host}:{settings.llm_chain_grpc_port}",
            },
            "cli_agent": {
                "healthy": cli_healthy,
                "host": f"{settings.cli_agent_grpc_host}:{settings.cli_agent_grpc_port}",
            },
        },
    }


# ────────────── Easy Trade Scanning ──────────────────────────────

OPPORTUNITY_CACHE_TTL = 1200  # 20 minutes


def _group_markets_by_event(markets: list) -> list:
    """
    Group a flat list of markets by their event slug.
    Returns a list of (event_slug, event_title, [markets]) tuples.
    Single-market events come first, multi-option events after.
    """

    groups: OrderedDict = OrderedDict()
    for m in markets:
        slug = m.get("_event_slug") or m.get("slug") or m.get("question", "solo")
        if slug not in groups:
            groups[slug] = {
                "slug": slug,
                "title": m.get("_event_title") or m.get("question", "Unknown"),
                "volume": m.get("_event_volume") or m.get("volume", "0"),
                "liquidity": m.get("_event_liquidity") or m.get("liquidity", "0"),
                "markets": [],
            }
        groups[slug]["markets"].append(m)
    return list(groups.values())


# ────────────── Shared event-group scoring ──────────────────────

#: Easy-trade entries returned by the batch pre-scan. Small on purpose:
#: the board ranks by ease, and a long tail of near-identical options
#: drowns the obvious one.
_EASY_TRADE_MAX_RESULTS = 10

#: Easy-trade fields merged onto a group's event score BEFORE the
#: per-sub-market fan-out. The event scan keeps its event-level verdict
#: (ai_score / risk_level / recommendation / reasoning); the batch call
#: contributes the per-option view of the single best option, which is
#: what the frontend board ranks on. ``ai_probability`` is the option's
#: own forecast, so it replaces the event-wide one.
_EASY_TRADE_FLOAT_FIELDS = (
    "ai_probability",
    "edge_estimate",
    "expected_probability",
    "confidence",
)
_EASY_TRADE_INT_FIELDS = ("ease_score",)
_EASY_TRADE_TEXT_FIELDS = ("recommended_option", "category")


def _as_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float coercion for Gamma values (str, None, junk)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_condition_id(group: dict) -> str:
    """Condition id of a group's first sub-market (the smart-money key)."""
    markets = group.get("markets") or []
    if not markets:
        return ""
    return markets[0].get("condition_id") or markets[0].get("conditionId") or ""


def _event_sub_markets(group: dict) -> list[dict]:
    """Flatten one event group into the sub-market rows the LLM sees.

    Shared by the per-group event scoring and the batch easy-trade
    pre-scan so both describe the same options in the same shape.
    """
    sub_markets: list[dict] = []
    for m in group.get("markets") or []:
        try:
            prices = m.get("outcomePrices", [])
            yes_p = float(prices[0]) if prices else 0.5
            no_p = float(prices[1]) if len(prices) > 1 else (1 - yes_p)
            vol24 = float(m.get("volume24hr") or 0)
            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
        except (TypeError, ValueError):
            yes_p, no_p, vol24, liq = 0.5, 0.5, 0.0, 0.0
        sub_markets.append(
            {
                "label": m.get("groupItemTitle") or m.get("question", ""),
                "question": m.get("question", ""),
                "condition_id": m.get("condition_id") or m.get("conditionId") or "",
                "yes_price": yes_p,
                "no_price": no_p,
                "volume_24h": vol24,
                "liquidity": liq,
            }
        )
    return sub_markets


async def _fetch_smart_money_context(service: Any, condition_id: str) -> tuple[str, dict]:
    """Smart-money prompt context for one group, plus the raw payload.

    The payload is kept because the credibility pass scores the whale
    fields (count, bias, order-book snapshot) that the flattened prompt
    context never carries.
    """
    if not condition_id:
        return "", {}
    try:
        sm = await service.get_smart_money_analysis(condition_id)
    except Exception:
        return "Smart-money data unavailable", {}
    payload = sm if isinstance(sm, dict) else {}
    # ``get_smart_money_analysis`` renders the order-book whale snapshot
    # into ``context_text`` (plus ``whale_count`` / ``whale_bias`` for the
    # credibility pass); reading any other key would send the model the
    # literal "No smart-money data" on every single scan.
    context = str(payload.get("context_text") or "").strip()
    return context or "No smart-money data", payload


async def _fetch_smart_money(event_groups: list[dict], service: Any) -> dict[str, tuple[str, dict]]:
    """Fetch every group's smart-money context concurrently, keyed by slug."""
    contexts = await asyncio.gather(
        *(_fetch_smart_money_context(service, _first_condition_id(g)) for g in event_groups)
    )
    return {g["slug"]: ctx for g, ctx in zip(event_groups, contexts, strict=True)}


async def _scan_event_easy_trades(
    event_groups: list[dict],
    client: AnalysisClient,
    llm_config: dict[str, Any] | None,
) -> dict[str, dict]:
    """One batch easy-trade call over the multi-option event groups.

    Returns a ``condition_id -> easy-trade entry`` index. An empty index
    on any failure (or when there are no multi-option groups): easy
    trades are a layer on top of the per-group event scoring, so a dead
    batch call must never fail the scan.
    """
    multi_groups = [g for g in event_groups if len(g.get("markets") or []) > 1]
    if not multi_groups:
        return {}

    events = [
        {"event_title": group["title"], "sub_markets": _event_sub_markets(group)}
        for group in multi_groups
    ]
    try:
        trades = await client.scan_event_easy_trades(
            events=events,
            max_results=_EASY_TRADE_MAX_RESULTS,
            llm_config=llm_config,
        )
    except Exception as exc:
        logger.info("Event easy trade scan unavailable (%s); keeping event scores", exc)
        return {}

    index: dict[str, dict] = {}
    if not isinstance(trades, list):
        return {}
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        condition_id = str(trade.get("condition_id") or "").strip()
        if condition_id and condition_id not in index:
            index[condition_id] = trade
    return index


def _best_group_easy_trade(sub_markets: list[dict], easy_index: dict[str, dict]) -> dict | None:
    """Highest-ease easy-trade entry that belongs to this event group."""
    best: dict | None = None
    for sm in sub_markets:
        entry = easy_index.get(sm["condition_id"])
        if entry is None:
            continue
        if best is None or _as_float(entry.get("ease_score")) > _as_float(best.get("ease_score")):
            best = entry
    return best


def _merge_event_easy_trade(event_scores: dict, entry: dict | None) -> dict:
    """Layer one group-wide easy-trade verdict onto its event score.

    Only the fields that describe the best option are taken, so a
    group's event-level risk / reasoning survive untouched.
    """
    if not entry:
        return event_scores
    merged = dict(event_scores)
    for field in _EASY_TRADE_FLOAT_FIELDS:
        value = _as_float(entry.get(field), default=float("nan"))
        if value == value:  # NaN/None/missing are all skipped
            merged[field] = value
    for field in _EASY_TRADE_INT_FIELDS:
        value = entry.get(field)
        if value is None:
            continue
        merged[field] = int(_as_float(value, default=0.0))
    for field in _EASY_TRADE_TEXT_FIELDS:
        value = entry.get(field)
        if value:
            merged[field] = str(value)
    return merged


def _apply_credibility(scores: dict, batch: BatchCredibility | None) -> dict:
    """Overlay the batch credibility verdict on one (sub-)market score.

    ``search_summary`` is only filled when the per-group LLM produced
    none, so a market-level research summary always wins.
    """
    if batch is None:
        return scores
    merged = dict(scores)
    merged["credibility_score"] = batch.credibility_score
    if batch.search_summary and not str(merged.get("search_summary") or "").strip():
        merged["search_summary"] = batch.search_summary
    return merged


async def _score_research_credibility(
    event_groups: list[dict],
    smart_by_key: dict[str, tuple[str, dict]],
    client: AnalysisClient,
    llm_config: dict[str, Any] | None,
) -> dict[str, BatchCredibility]:
    """Score the batch's research credibility: one synthesis call plus a
    capped number of per-source evaluations, never a failed scan."""
    if not event_groups:
        return {}
    evidence = [
        GroupEvidence(
            key=group["slug"],
            question=str(group.get("title") or ""),
            markets=group.get("markets") or [],
            smart_money=smart_by_key.get(group["slug"], ("", {}))[1],
            priority=_as_float(group.get("volume")),
        )
        for group in event_groups
    ]
    try:
        return await score_batch_credibility(
            client=client,
            groups=evidence,
            llm_config=llm_config,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.info("Research credibility scoring skipped: %s", exc)
        return {}


async def _score_event_groups(
    *,
    event_groups: list[dict],
    client: AnalysisClient,
    llm_config: dict[str, Any] | None,
    service: Any,
    label: str = "opportunity-scan",
) -> tuple[list[asyncio.Task], asyncio.Queue]:
    """Analyse every event group, streaming results onto a queue.

    The single implementation of the per-group pipeline, shared by
    ``stream_analyze_markets`` and ``stream_opportunity_scan`` so the two
    endpoints cannot diverge. ``label`` only shapes log messages.

    Three batch-level upgrades run before the per-group fan-out, all of
    them non-fatal:

    1. each group's smart-money context is fetched concurrently (same
       fan-out as before, now up front so the credibility pass can read
       the whale fields);
    2. ONE ``scan_event_easy_trades`` call over the multi-option groups,
       merged into each group's event score before its sub-markets are
       fanned out (each sub-market keeps its own ``condition_id``);
    3. ONE ``synthesize_research`` call plus capped per-source
       ``source_evaluation`` calls, producing the batch's
       ``credibility_score`` / ``search_summary``.
    """
    result_queue: asyncio.Queue = asyncio.Queue()
    semaphore = asyncio.Semaphore(_SCAN_CONCURRENCY)

    smart_by_key = await _fetch_smart_money(event_groups, service)
    # The last two phases are independent of each other, so they run
    # concurrently: the scan waits for the slower one, not for their sum.
    # Both swallow their own failures and degrade to the per-group
    # scores, so ``gather`` never raises here.
    easy_index, credibility = await asyncio.gather(
        _scan_event_easy_trades(event_groups, client, llm_config),
        _score_research_credibility(event_groups, smart_by_key, client, llm_config),
    )

    async def _analyse_group(group: dict):
        """Analyse one event group under the semaphore."""
        group_markets = group["markets"]
        event_title = group["title"]
        event_vol = str(group.get("volume", "0"))
        event_liq = str(group.get("liquidity", "0"))
        smart_ctx = smart_by_key.get(group["slug"], ("", {}))[0]

        is_single = len(group_markets) == 1

        async with semaphore:
            if is_single:
                m = group_markets[0]
                first_cid = m.get("condition_id") or m.get("conditionId") or ""
                prices = m.get("outcomePrices", [])
                try:
                    yes_p = float(prices[0]) if prices else 0.5
                    no_p = float(prices[1]) if len(prices) > 1 else (1 - yes_p)
                    vol24 = float(m.get("volume24hr") or m.get("_event_volume_24hr") or 0)
                    liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
                except (ValueError, TypeError):
                    yes_p, no_p = 0.5, 0.5
                    vol24, liq = 0.0, 0.0

                end_d = m.get("end_date_iso") or m.get("endDateIso") or ""
                pnl = round(max(yes_p, no_p) - min(yes_p, no_p), 4)

                try:
                    scores = await client.scan_opportunity(
                        market_title=m.get("question", "Unknown"),
                        market_description=m.get("description", ""),
                        yes_price=yes_p,
                        no_price=no_p,
                        volume_24h=vol24,
                        end_date=end_d,
                        pnl_potential=pnl,
                        smart_money_context=smart_ctx,
                        liquidity=liq,
                        condition_id=first_cid,
                        llm_config=llm_config,
                    )
                except Exception as exc:
                    logger.error("[%s] scan_opportunity failed for %s: %s", label, first_cid, exc)
                    scores = {
                        "condition_id": first_cid,
                        "market_title": m.get("question", "Unknown"),
                        "ai_score": 30,
                        "risk_level": "high",
                        "pnl_potential": pnl,
                        "credibility_score": 30,
                        "smart_money_signal": "neutral",
                        "smart_money_summary": "",
                        "recommendation": "hold",
                        "recommended_side": "YES" if yes_p < 0.5 else "NO",
                        "reasoning": "AI analysis failed",
                        "search_summary": "",
                        "key_risks": [],
                    }

                await result_queue.put(
                    (
                        first_cid,
                        _apply_credibility(scores, credibility.get(group["slug"])),
                    )
                )
            else:
                sub_markets = _event_sub_markets(group)

                try:
                    event_scores = await client.scan_event_opportunity(
                        event_title=event_title,
                        sub_markets=sub_markets,
                        event_volume=event_vol,
                        event_liquidity=event_liq,
                        smart_money_context=smart_ctx,
                        llm_config=llm_config,
                    )
                except Exception as exc:
                    logger.error(
                        "[%s] scan_event_opportunity failed for %s: %s", label, event_title, exc
                    )
                    event_scores = {
                        "event_title": event_title,
                        "ai_score": 30,
                        "risk_level": "high",
                        "pnl_potential": 0.5,
                        "credibility_score": 30,
                        "smart_money_signal": "neutral",
                        "smart_money_summary": "",
                        "recommendation": "hold",
                        "recommended_side": "YES",
                        "recommended_option": "",
                        "reasoning": "AI analysis failed",
                        "search_summary": "",
                        "key_risks": [],
                    }

                # Easy trades describe the best option of the event; every
                # other sub-market keeps the event-wide score but its own
                # condition_id, which is what the frontend merges on.
                best_easy_trade = _best_group_easy_trade(sub_markets, easy_index)
                event_scores = _merge_event_easy_trade(event_scores, best_easy_trade)
                event_scores = _apply_credibility(event_scores, credibility.get(group["slug"]))

                # The merged probability fields describe exactly ONE option
                # of the group: the easy trade's own sub-market (the event
                # prompt asks for "the recommended option's" probability).
                # Fanning them out verbatim would record that option's
                # forecast under every other option's condition_id, so only
                # the owning sub-market keeps them and record_verdict
                # writes nothing for the rest (calibration poisoning).
                probability_owner = (
                    str(best_easy_trade.get("condition_id") or "").strip()
                    if best_easy_trade
                    else ""
                )

                # Fan out the event-level scores to each sub-market
                for sm in sub_markets:
                    cid = sm["condition_id"]
                    per_market = {**event_scores}
                    per_market["condition_id"] = cid
                    per_market["market_title"] = sm.get("question") or sm.get("label", "")
                    if cid != probability_owner:
                        per_market["ai_probability"] = None
                        per_market["expected_probability"] = None
                    await result_queue.put((cid, per_market))

    tasks = [asyncio.create_task(_analyse_group(g)) for g in event_groups]
    return tasks, result_queue


@router.post(
    "/opportunities/analyze-markets",
    dependencies=[
        Depends(_analysis_scan_rate_limit),
        Depends(optional_plan_quota(CAP_OPPORTUNITY_SCAN)),
    ],
)
async def stream_analyze_markets(
    request: AnalyzeMarketsRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Analyze a specific batch of markets via SSE (public).

    Accepts pre-fetched market data, runs the same per-market AI analysis
    pipeline, and streams scores back progressively. This avoids re-fetching
    markets from Gamma since the frontend already has them.
    """
    user_id = current_user.get("user_id") if current_user else None
    service = get_polymarket_service()

    async def event_generator():
        markets = request.markets
        if not markets:
            yield f"data: {json.dumps({'all_done': True, 'total': 0})}\n\n"
            return

        yield f"data: {json.dumps({'status': 'analyzing', 'total': len(markets)})}\n\n"

        event_groups = _group_markets_by_event(markets)
        total = len(markets)
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        tasks: list = []
        result_queue: asyncio.Queue = asyncio.Queue()

        # ``BaseException`` (not just ``Exception``) is caught so a client
        # disconnect mid-batch — which surfaces as ``CancelledError``
        # inside ``_score_event_groups`` — still runs the ``finally`` and
        # closes this request's channel instead of leaking it. The guard
        # re-raises, so the existing SSE-error semantics are unchanged.
        try:
            try:
                tasks, result_queue = await _score_event_groups(
                    event_groups=event_groups,
                    client=client,
                    llm_config=llm_config,
                    service=service,
                    label="analyze-markets",
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.error("analyze-markets failed to start: %s", e, exc_info=True)
                yield f"data: {json.dumps({'error': 'Analysis failed'})}\n\n"
                return

            all_results = []
            done_count = 0

            while done_count < total:
                try:
                    cid, scores = await asyncio.wait_for(result_queue.get(), timeout=2.0)
                    all_results.append(scores)
                    done_count += 1
                    # ── Forecast calibration (plan 01) ──────────
                    # Capture one verdict per emitted score with the
                    # market price at decision time; a recorder failure
                    # must never fail a scan.
                    with contextlib.suppress(Exception):
                        backend = (
                            "cli_agent" if client.backend == AIBackend.CLI_AGENT else "llm_chain"
                        )
                        record_verdict(
                            db,
                            condition_id=cid,
                            task="analyze-markets",
                            model_name=scores.get("model")
                            or ("unknown" if backend == "cli_agent" else ""),
                            provider=scores.get("provider") or backend,
                            ai_probability=scores.get("ai_probability"),
                            market_price=next(
                                (
                                    (m.get("outcomePrices") or [None])[0]
                                    for m in markets
                                    if (m.get("condition_id") or m.get("conditionId") or "") == cid
                                    and m.get("outcomePrices")
                                ),
                                None,
                            ),
                            ai_score=scores.get("ai_score"),
                            confidence=scores.get("confidence"),
                            recommendation=scores.get("recommendation"),
                            source="analyze_markets",
                        )
                    payload = json.dumps(
                        {
                            "market_id": cid,
                            "scores": scores,
                            "done_count": done_count,
                            "total": total,
                        }
                    )
                    yield f"data: {payload}\n\n"
                except TimeoutError:
                    if all(t.done() for t in tasks) and result_queue.empty():
                        break
                    continue

            yield f"data: {json.dumps({'all_done': True, 'total': len(all_results)})}\n\n"
        except Exception as e:
            logger.error("analyze-markets stream error: %s", e, exc_info=True)
            yield f"data: {json.dumps({'error': 'Analysis failed'})}\n\n"
        except BaseException:
            # Client disconnect / cancellation: close the channel below,
            # then let the cancellation propagate so streaming shutdown
            # behaves exactly as before.
            raise
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
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


@router.post(
    "/opportunities/stream",
    dependencies=[
        Depends(_analysis_scan_rate_limit),
        Depends(optional_plan_quota(CAP_OPPORTUNITY_SCAN)),
    ],
)
async def stream_opportunity_scan(
    request: OpportunityScanRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    AI-powered Easy Trade finder via SSE.

    1. Fetches the NEWEST markets from Polymarket (sorted by creation date)
    2. Groups markets by event slug
    3. For each group, fetches smart-money data and runs per-market AI analysis
       (scan_opportunity for single markets, scan_event_opportunity for groups)
    4. Streams per-market scores back to the frontend for progressive display

    Uses bounded concurrency (semaphore + queue) so the LLM backend
    is not overwhelmed.

    Results are cached for 20 minutes unless force_refresh is True.

    SSE event format per market:
      data: {"market_id": "...", "scores": {...}, "done_count": N, "total": M}
    Final:
      data: {"all_done": true, "total": N}
    """
    user_id = current_user.get("user_id") if current_user else None
    service = get_polymarket_service()

    async def event_generator():
        # ── Check cache ──────────────────────────────────────
        cache_key = f"easy_trades:scan:{user_id}:{request.limit}"
        if not request.force_refresh:
            cached = opportunity_cache.get(cache_key)
            if cached:
                logger.info("Serving easy trade scan from cache for user %s", user_id)
                for i, r in enumerate(cached):
                    payload = json.dumps(
                        {
                            "market_id": r.get("condition_id", ""),
                            "scores": r,
                            "done_count": i + 1,
                            "total": len(cached),
                            "cached": True,
                        }
                    )
                    yield f"data: {payload}\n\n"
                done_payload = json.dumps({"all_done": True, "total": len(cached), "cached": True})
                yield f"data: {done_payload}\n\n"
                return

        # ── Fetch combined (newest + trending) markets ────────
        try:
            yield f"data: {json.dumps({'status': 'fetching_markets'})}\n\n"
            newest_task = service.get_newest_markets(limit=request.limit)
            trending_task = service.get_active_markets(limit=request.limit)
            newest, trending = await asyncio.gather(newest_task, trending_task)

            # Deduplicate: newest first, then trending extras
            seen: set = set()
            markets: list = []
            for m in newest:
                cid = m.get("condition_id") or m.get("conditionId") or m.get("id", "")
                if cid and cid not in seen:
                    seen.add(cid)
                    markets.append(m)
            for m in trending:
                cid = m.get("condition_id") or m.get("conditionId") or m.get("id", "")
                if cid and cid not in seen:
                    seen.add(cid)
                    markets.append(m)

            if not markets:
                yield f"data: {json.dumps({'error': 'No markets found'})}\n\n"
                return
            yield f"data: {json.dumps({'status': 'markets_loaded', 'total': len(markets)})}\n\n"
        except Exception as e:
            logger.error("Failed to fetch markets: %s", e)
            yield f"data: {json.dumps({'error': 'Failed to fetch markets'})}\n\n"
            return

        # ── Group by event & create analysis client ──────────
        event_groups = _group_markets_by_event(markets)
        total = len(markets)
        client, llm_config = await get_analysis_client_for_user(user_id, db)

        # Per-group analysis queue, populated by the shared scorer.
        tasks: list = []
        result_queue: asyncio.Queue = asyncio.Queue()

        # ``BaseException`` (not just ``Exception``) is caught so a client
        # disconnect mid-batch — which surfaces as ``CancelledError``
        # inside ``_score_event_groups`` — still runs the ``finally`` and
        # closes this request's channel instead of leaking it. The guard
        # re-raises, so the existing SSE-error semantics are unchanged.
        try:
            try:
                tasks, result_queue = await _score_event_groups(
                    event_groups=event_groups,
                    client=client,
                    llm_config=llm_config,
                    service=service,
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Easy trade scan failed to start: %s", e, exc_info=True)
                yield f"data: {json.dumps({'error': 'Analysis failed'})}\n\n"
                return

            all_results = []
            done_count = 0

            while done_count < total:
                # Wait for next result or check if all tasks done
                try:
                    cid, scores = await asyncio.wait_for(result_queue.get(), timeout=2.0)
                    all_results.append(scores)
                    done_count += 1
                    # ── Forecast calibration (plan 01) ──────────
                    # Capture one verdict per emitted score with the
                    # market price at decision time; a recorder failure
                    # must never fail a scan.
                    with contextlib.suppress(Exception):
                        backend = (
                            "cli_agent" if client.backend == AIBackend.CLI_AGENT else "llm_chain"
                        )
                        record_verdict(
                            db,
                            condition_id=cid,
                            task="opportunity-scan",
                            model_name=scores.get("model")
                            or ("unknown" if backend == "cli_agent" else ""),
                            provider=scores.get("provider") or backend,
                            ai_probability=scores.get("ai_probability"),
                            market_price=next(
                                (
                                    (m.get("outcomePrices") or [None])[0]
                                    for m in markets
                                    if (m.get("condition_id") or m.get("conditionId") or "") == cid
                                    and m.get("outcomePrices")
                                ),
                                None,
                            ),
                            ai_score=scores.get("ai_score"),
                            confidence=scores.get("confidence"),
                            recommendation=scores.get("recommendation"),
                            source="opportunity_scan",
                        )
                    payload = json.dumps(
                        {
                            "market_id": cid,
                            "scores": scores,
                            "done_count": done_count,
                            "total": total,
                        }
                    )
                    yield f"data: {payload}\n\n"
                except TimeoutError:
                    # Check if all tasks are done (maybe some failed silently)
                    if all(t.done() for t in tasks) and result_queue.empty():
                        break
                    continue

            # ── Sort & cache ─────────────────────────────────
            all_results.sort(key=lambda x: x.get("ai_score", 0), reverse=True)
            opportunity_cache.set(cache_key, all_results, ttl_seconds=OPPORTUNITY_CACHE_TTL)

            yield f"data: {json.dumps({'all_done': True, 'total': len(all_results)})}\n\n"

        except Exception as e:
            logger.error("Easy trade scan stream error: %s", e, exc_info=True)
            yield f"data: {json.dumps({'error': 'Analysis failed'})}\n\n"
        except BaseException:
            # Client disconnect / cancellation: close the channel below,
            # then let the cancellation propagate so streaming shutdown
            # behaves exactly as before.
            raise
        finally:
            # Cancel any remaining tasks
            for t in tasks:
                if not t.done():
                    t.cancel()
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
