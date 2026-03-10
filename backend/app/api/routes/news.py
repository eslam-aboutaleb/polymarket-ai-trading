"""
News API routes – on-demand and cached AI-generated news articles.

Endpoints:
  GET  /api/news/feed                  – cached feed (newest first)
  GET  /api/news/market/{condition_id} – cached article for one market
  POST /api/news/generate              – generate article on demand
  POST /api/news/generate/stream       – SSE stream while generating
  POST /api/news/refresh-feed          – regenerate feed for trending markets
"""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.news_service import get_news_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])


# ── Schemas ─────────────────────────────────────────────────

class GenerateNewsRequest(BaseModel):
    condition_id: str = Field(..., description="Market condition ID")
    question: str = Field(..., description="Market question text")
    force: bool = Field(False, description="Bypass cache and regenerate")
    provider: Optional[str] = Field(None, description="LLM provider override")
    model: Optional[str] = Field(None, description="LLM model override")


class RefreshFeedRequest(BaseModel):
    max_markets: int = Field(5, ge=1, le=20, description="Number of markets")
    provider: Optional[str] = None
    model: Optional[str] = None


# ── Endpoints ───────────────────────────────────────────────

@router.get("/feed")
async def get_news_feed(limit: int = Query(20, ge=1, le=50)):
    """Return the most recent cached news articles."""
    svc = get_news_service()
    feed = svc.get_cached_feed(limit=limit)
    return {"articles": feed, "count": len(feed)}


@router.get("/market/{condition_id}")
async def get_market_news(condition_id: str):
    """Return the cached news article for a specific market."""
    svc = get_news_service()
    article = svc.get_for_market(condition_id)
    if article is None:
        return {"article": None, "cached": False}
    return {"article": article, "cached": True}


@router.post("/generate")
async def generate_news(body: GenerateNewsRequest):
    """Generate a news article for a market (on-demand)."""
    svc = get_news_service()
    try:
        article = await svc.generate_for_market(
            condition_id=body.condition_id,
            question=body.question,
            force=body.force,
            provider=body.provider,
            model=body.model,
        )
        return {"article": article, "cached": False}
    except Exception as exc:
        logger.exception("News generation failed for %s", body.condition_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/generate/stream")
async def generate_news_stream(body: GenerateNewsRequest):
    """SSE endpoint that streams progress while generating a news article."""

    async def event_stream():
        svc = get_news_service()
        yield _sse_event("status", {"message": "Starting news generation…"})
        try:
            article = await svc.generate_for_market(
                condition_id=body.condition_id,
                question=body.question,
                force=body.force,
                provider=body.provider,
                model=body.model,
            )
            yield _sse_event("article", article)
            yield _sse_event("done", {"message": "Generation complete"})
        except Exception as exc:
            yield _sse_event("error", {"message": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/refresh-feed")
async def refresh_feed(body: RefreshFeedRequest):
    """Regenerate the news feed for trending markets."""
    svc = get_news_service()
    try:
        articles = await svc.generate_for_trending(
            max_markets=body.max_markets,
            provider=body.provider,
            model=body.model,
        )
        return {"articles": articles, "count": len(articles)}
    except Exception as exc:
        logger.exception("Feed refresh failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ── Helpers ─────────────────────────────────────────────────

def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"
