"""Markets browsing & AI trader-analysis API routes."""

import json
import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth import get_optional_user_from_token
from app.config import AIBackend, get_settings
from app.grpc_clients.analysis_client import AnalysisClient
from app.models.user_settings import AIBackendType, UserSettings
from app.services.polymarket_service import get_polymarket_service
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(prefix="/api/markets", tags=["markets"])
settings = get_settings()
logger = logging.getLogger(__name__)


# ── Request / Response schemas ───────────────────────────────────────


class TraderAnalysisRequest(BaseModel):
    """Request body for streaming trader analysis."""

    condition_id: str = Field(..., description="Market condition ID (hex)")
    question: str = Field(..., description="Market question text")
    yes_price: float = Field(default=0.5, ge=0, le=1)
    no_price: float = Field(default=0.5, ge=0, le=1)
    volume_24h: float = Field(default=0)
    end_date: str = Field(default="", description="Market end date ISO")


# ── Helpers ──────────────────────────────────────────────────────────


async def _get_analysis_client(user_id: int | None, db: Session) -> AnalysisClient:
    """Return an AnalysisClient configured for the user's preferred backend."""
    if user_id is not None:
        record = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
        backend = (
            AIBackend.CLI_AGENT
            if (record and record.ai_backend == AIBackendType.CLI_AGENT.value)
            else AIBackend.LLM_CHAIN
        )
    else:
        backend = AIBackend.LLM_CHAIN
    return AnalysisClient(backend=backend)


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/categories")
async def get_categories():
    """Return the list of Polymarket market categories (public)."""
    service = get_polymarket_service()
    return {"categories": service.get_market_categories()}


@router.get("/search")
async def search_markets(
    q: str = Query("", description="Search term (matches question / event title)"),
    tag: str = Query("", description="Optional category tag filter"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    sort: str = Query("volume24hr", description="Sort field: volume24hr | liquidity | startDate"),
):
    """Search / browse all active Polymarket markets (public)."""
    service = get_polymarket_service()
    return await service.search_all_markets(
        query=q,
        tag=tag,
        limit=limit,
        offset=offset,
        sort=sort,
    )


@router.get("/browse")
async def browse_markets(
    tag: str = Query(..., description="Category tag, e.g. 'crypto'"),
    limit: int = Query(60, ge=1, le=100),
):
    """Browse active markets filtered by category tag (public)."""
    service = get_polymarket_service()
    markets = await service.get_markets_by_category(tag=tag, limit=limit)
    return {"tag": tag, "markets": markets, "count": len(markets)}


@router.post("/trader-analysis/stream")
async def stream_trader_analysis(
    request: TraderAnalysisRequest,
    current_user: dict | None = Depends(get_optional_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Stream an AI-powered trader-positioning analysis for a market via SSE.

    1. Fetches recent trades on the market from the CLOB API.
    2. Aggregates trader statistics (unique traders, volume, top traders per side).
    3. Passes enriched context to the LLM for a streamed analysis that highlights
       how many winning/experienced traders are betting on each position.
    """
    service = get_polymarket_service()
    user_id = current_user.get("user_id") if current_user else None
    client = await _get_analysis_client(user_id, db)

    # ── 1. Gather trader statistics ──────────────────────────────
    trader_stats = await service.get_market_trader_stats(request.condition_id)

    # ── 2. Build an enriched prompt for the LLM ─────────────────
    top_traders_text = ""
    for t in trader_stats.get("top_traders", [])[:8]:
        top_traders_text += (
            f"  • {t['short_address']}  —  total ${t['total_volume']:.2f}  "
            f"(YES ${t['yes_volume']:.2f} / NO ${t['no_volume']:.2f})  "
            f"lean: {t['lean']}\n"
        )

    stats_context = (
        f"MARKET: {request.question}\n"
        f"Current prices — YES: {request.yes_price * 100:.1f}¢  NO: {request.no_price * 100:.1f}¢\n"
        f"24h volume: ${request.volume_24h:,.0f}\n"
        f"End date: {request.end_date}\n\n"
        f"TRADER POSITIONING DATA (from Polymarket CLOB):\n"
        f"  Total trades recorded: {trader_stats['total_trades']}\n"
        f"  YES-side traders: {trader_stats['yes_traders']}"
        f"  |  volume: ${trader_stats['yes_volume']:,.2f}\n"
        f"  NO-side  traders: {trader_stats['no_traders']}"
        f"  |  volume: ${trader_stats['no_volume']:,.2f}\n"
        f"  Volume split — YES {trader_stats['side_ratio']['yes']:.1f}%"
        f"  /  NO {trader_stats['side_ratio']['no']:.1f}%\n\n"
        f"TOP TRADERS (by volume on this market):\n{top_traders_text}\n"
    )

    # We augment the existing market analysis prompt with trader data
    enriched_description = (
        f"{request.question}\n\n"
        f"--- TRADER INTELLIGENCE ---\n{stats_context}\n"
        f"Please analyze HOW traders are positioning on this market. "
        f"Search the web for current news about this topic to enhance your analysis.\n\n"
        f"IMPORTANT: You MUST respond with a valid JSON object and NOTHING ELSE — no markdown, "
        f"no explanation before or after the JSON. Use this exact schema:\n"
        f"{{\n"
        f'  "market_summary": "Brief overview of the market and what is being predicted",\n'
        f'  "probability_assessment": "Your assessed probability (e.g. 65%) '
        f'with brief reasoning",\n'
        f'  "confidence_level": "Low / Medium / High — with brief justification",\n'
        f'  "trader_positioning": "Which side has more volume/traders and what it means",\n'
        f'  "smart_money_analysis": "What top/winning traders are doing '
        f'and which side they lean",\n'
        f'  "key_factors": ["Factor 1", "Factor 2", "Factor 3"],\n'
        f'  "recommendation": "BUY YES / BUY NO / HOLD — with reasoning",\n'
        f'  "risk_factors": ["Risk 1", "Risk 2", "Risk 3"],\n'
        f'  "conclusion": "Final summary tying everything together"\n'
        f"}}\n"
    )

    # ── 3. Stream the analysis via SSE ───────────────────────────
    async def event_generator():
        # First emit the raw trader stats as a structured event
        stats_payload = json.dumps(
            {
                "trader_stats": {
                    "yes_traders": trader_stats["yes_traders"],
                    "no_traders": trader_stats["no_traders"],
                    "yes_volume": trader_stats["yes_volume"],
                    "no_volume": trader_stats["no_volume"],
                    "total_trades": trader_stats["total_trades"],
                    "side_ratio": trader_stats["side_ratio"],
                    "top_traders": trader_stats["top_traders"][:6],
                }
            }
        )
        yield f"data: {stats_payload}\n\n"

        try:
            async for chunk in client.analyze_market_stream(
                market_title=request.question,
                market_description=enriched_description,
                yes_price=request.yes_price,
                no_price=request.no_price,
                volume_24h=request.volume_24h,
                end_date=request.end_date or utc_now().isoformat(),
                include_research=True,
            ):
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.error("Trader analysis stream error: %s", e, exc_info=True)
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
