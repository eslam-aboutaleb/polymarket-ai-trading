"""
News Service – generates and caches AI-powered news articles for markets.

Articles are ephemeral (in-memory cache with TTL, no database persistence).
Backs both the on-demand API and the periodic background generator.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.utils.cache import get_cache

logger = logging.getLogger(__name__)
settings = get_settings()

# Cache with 20-minute TTL for news articles
news_cache = get_cache("news")

# Background task handle
_generator_task: Optional[asyncio.Task] = None

# Default generation interval (seconds)
_GENERATION_INTERVAL = 600  # 10 minutes


class NewsService:
    """Generate, cache, and retrieve AI-powered news articles."""

    # ── Public API ──────────────────────────────────────────────

    async def generate_for_market(
        self,
        condition_id: str,
        question: str,
        *,
        force: bool = False,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate a news article for a single market.

        Returns the article dict (headline, summary, body, …).
        Caches the result keyed by condition_id.
        """
        cache_key = f"article:{condition_id}"

        if not force:
            cached = news_cache.get(cache_key)
            if cached:
                logger.debug("News cache hit for %s", condition_id)
                return cached

        from app.grpc_clients.analysis_client import get_analysis_client

        client = await get_analysis_client()
        article = await client.generate_news(
            condition_id=condition_id,
            question=question,
            provider=provider,
            model=model,
        )

        # Store with 20-min TTL
        news_cache.set(cache_key, article, ttl=1200)

        # Also append to feed cache
        self._append_to_feed(article)

        return article

    def get_for_market(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Return the cached article for a market, or None."""
        return news_cache.get(f"article:{condition_id}")

    def get_cached_feed(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent cached articles (newest first)."""
        feed: List[Dict[str, Any]] = news_cache.get("feed") or []
        return feed[:limit]

    async def generate_for_trending(
        self,
        *,
        max_markets: int = 5,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Generate articles for the top trending markets."""
        markets = await self._fetch_trending_markets(max_markets)
        articles: List[Dict[str, Any]] = []

        for mkt in markets:
            try:
                article = await self.generate_for_market(
                    condition_id=mkt["condition_id"],
                    question=mkt["question"],
                    provider=provider,
                    model=model,
                )
                articles.append(article)
            except Exception as exc:
                logger.warning(
                    "Failed to generate news for %s: %s",
                    mkt.get("question", mkt["condition_id"]),
                    exc,
                )
        return articles

    # ── Internals ───────────────────────────────────────────────

    def _append_to_feed(self, article: Dict[str, Any]) -> None:
        """Prepend an article to the global feed cache (capped at 50)."""
        feed: List[Dict[str, Any]] = news_cache.get("feed") or []

        # Deduplicate by condition_id
        cid = article.get("condition_id")
        feed = [a for a in feed if a.get("condition_id") != cid]
        feed.insert(0, article)
        feed = feed[:50]

        news_cache.set("feed", feed, ttl=3600)  # 1-hour feed TTL

    async def _fetch_trending_markets(self, limit: int) -> List[Dict[str, Any]]:
        """Get top active markets from Polymarket to generate news for."""
        try:
            from app.services.polymarket_service import PolymarketService

            pm = PolymarketService()
            markets_raw = await pm.get_markets(limit=limit, active=True)
            return [
                {
                    "condition_id": m.get("condition_id", ""),
                    "question": m.get("question", ""),
                }
                for m in (markets_raw or [])
                if m.get("condition_id")
            ]
        except Exception as exc:
            logger.warning("Could not fetch trending markets: %s", exc)
            return []


# ── Module-level singleton ──────────────────────────────────

_news_service: Optional[NewsService] = None


def get_news_service() -> NewsService:
    global _news_service
    if _news_service is None:
        _news_service = NewsService()
    return _news_service


# ── Background generator ────────────────────────────────────

async def _news_generation_loop() -> None:
    """Periodically generate news for trending markets."""
    svc = get_news_service()
    while True:
        try:
            logger.info("Background news generation: starting cycle")
            articles = await svc.generate_for_trending(max_markets=5)
            logger.info(
                "Background news generation: produced %d articles", len(articles)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Background news generation error: %s", exc)
        await asyncio.sleep(_GENERATION_INTERVAL)


async def start_news_generator() -> None:
    """Start the periodic news background task."""
    global _generator_task
    if _generator_task is not None:
        return
    _generator_task = asyncio.create_task(_news_generation_loop())
    logger.info("News background generator started")


async def stop_news_generator() -> None:
    """Cancel the periodic news background task."""
    global _generator_task
    if _generator_task is not None:
        _generator_task.cancel()
        try:
            await _generator_task
        except asyncio.CancelledError:
            pass
        _generator_task = None
        logger.info("News background generator stopped")
