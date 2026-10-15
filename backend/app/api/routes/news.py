"""
News API routes – on-demand and cached AI-generated news articles.

Endpoints:
  GET  /api/news/feed                  – cached feed (newest first)
  GET  /api/news/market/{condition_id} – cached article for one market
  POST /api/news/generate              – generate article on demand
  POST /api/news/generate/stream       – SSE stream while generating
  POST /api/news/refresh-feed          – regenerate feed for trending markets

The two GET routes stay public: they only read already-generated cached
articles for markets whose data is public on Polymarket, and the service
calls take no user context, so an optional-user dependency would enrich
nothing. The three POST routes trigger paid LLM generation and are
therefore authenticated (cookie or bearer token) and rate limited
below the global 100/minute budget.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import require_plan_quota
from app.api.routes.auth import get_current_user_from_token
from app.security.rate_limit import limiter
from app.services.entitlement_service import CAP_NEWS_GENERATION
from app.services.news_service import get_news_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])


# ── Schemas ─────────────────────────────────────────────────


class GenerateNewsRequest(BaseModel):
    condition_id: str = Field(..., description="Market condition ID")
    question: str = Field(..., description="Market question text")
    force: bool = Field(False, description="Bypass cache and regenerate")
    provider: str | None = Field(None, description="LLM provider override")
    model: str | None = Field(None, description="LLM model override")


class RefreshFeedRequest(BaseModel):
    max_markets: int = Field(5, ge=1, le=20, description="Number of markets")
    provider: str | None = None
    model: str | None = None


# Generation hits paid LLM providers, so the POST routes both require
# authentication and carry a per-endpoint budget well below the global
# 100/minute. The limit rides on a stub dependency (the auth.py pattern):
# each stub takes the starlette Request, so the limiter keys off the
# client IP without touching the handler signatures.
@limiter.limit("5/minute")
def _news_generate_rate_limit(request: Request) -> None:
    """Rate-limit POST /api/news/generate: 5/minute per IP."""


@limiter.limit("5/minute")
def _news_generate_stream_rate_limit(request: Request) -> None:
    """Rate-limit POST /api/news/generate/stream: 5/minute per IP."""


@limiter.limit("5/minute")
def _news_refresh_feed_rate_limit(request: Request) -> None:
    """Rate-limit POST /api/news/refresh-feed: 5/minute per IP."""


# ── Endpoints ───────────────────────────────────────────────


@router.get("/feed")
async def get_news_feed(limit: int = Query(20, ge=1, le=50)):
    """Return the most recent cached news articles.

    Public by design (see the module docstring): the read is served from
    the article cache, and the service takes no user context, so an
    optional-user dependency would add nothing.
    """
    svc = get_news_service()
    feed = svc.get_cached_feed(limit=limit)
    return {"articles": feed, "count": len(feed)}


@router.get("/market/{condition_id}")
async def get_market_news(condition_id: str):
    """Return the cached news article for a specific market.

    Public for the same reason as ``/feed``: cached, market-level data
    with no user-specific behaviour to gate.
    """
    svc = get_news_service()
    article = svc.get_for_market(condition_id)
    if article is None:
        return {"article": None, "cached": False}
    return {"article": article, "cached": True}


@router.post(
    "/generate",
    dependencies=[
        Depends(_news_generate_rate_limit),
        Depends(require_plan_quota(CAP_NEWS_GENERATION)),
    ],
)
async def generate_news(
    body: GenerateNewsRequest,
    current_user: dict = Depends(get_current_user_from_token),
):
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
        raise HTTPException(status_code=502, detail="News generation failed") from exc


@router.post(
    "/generate/stream",
    dependencies=[
        Depends(_news_generate_stream_rate_limit),
        Depends(require_plan_quota(CAP_NEWS_GENERATION)),
    ],
)
async def generate_news_stream(
    body: GenerateNewsRequest,
    current_user: dict = Depends(get_current_user_from_token),
):
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
        except Exception:
            logger.exception("News generation failed for %s", body.condition_id)
            yield _sse_event("error", {"message": "News generation failed"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post(
    "/refresh-feed",
    dependencies=[
        Depends(_news_refresh_feed_rate_limit),
        Depends(require_plan_quota(CAP_NEWS_GENERATION)),
    ],
)
async def refresh_feed(
    body: RefreshFeedRequest,
    current_user: dict = Depends(get_current_user_from_token),
):
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
        raise HTTPException(status_code=502, detail="News feed refresh failed") from exc


# ── Helpers ─────────────────────────────────────────────────


def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"
