"""
News Service – generates and caches AI-powered news articles for markets.

Articles are ephemeral (in-memory cache with TTL, no database persistence).
Backs both the on-demand API and the periodic background generator.
"""

import asyncio
import contextlib
import logging
from typing import Any

from app.config import AIBackend, get_settings
from app.services.llm_gateway import PROVIDER_TO_PROTO, LLMProviderType
from app.utils.cache import get_cache

logger = logging.getLogger(__name__)
settings = get_settings()

# Cache with 20-minute TTL for news articles
news_cache = get_cache("news")

# Background task handle
_generator_task: asyncio.Task | None = None

# Default generation interval (seconds)
_GENERATION_INTERVAL = 600  # 10 minutes


def _resolve_news_backend(provider: str | None) -> AIBackend:
    """Resolve the analysis backend for a news provider override.

    News always runs on llm-chain: it is the only backend whose
    servicer implements the ``GenerateNews`` RPC (the CLI agent's
    generated servicer base raises ``UNIMPLEMENTED``), and llm-chain
    honours every provider through the per-request ``llm_config``, so
    the provider/model override still takes effect. The provider is
    validated only to keep the invalid-provider warning.
    """
    if provider:
        try:
            LLMProviderType(provider.lower())
        except ValueError:
            logger.warning("Invalid LLM provider for news generation: %s", provider)
    return AIBackend.LLM_CHAIN


def _build_news_llm_config(provider: str | None, model: str | None) -> dict[str, Any] | None:
    """Build the per-request LLM config for a news gRPC call.

    Maps the provider string to its proto enum value via the
    gateway's ``PROVIDER_TO_PROTO`` table. An absent or invalid
    provider yields ``None`` (no override — the service default
    applies).
    """
    if not provider:
        return None
    try:
        provider_enum = LLMProviderType(provider.lower())
    except ValueError:
        return None
    config: dict[str, Any] = {"provider": PROVIDER_TO_PROTO[provider_enum]}
    if model:
        config["model"] = model
    return config


class NewsService:
    """Generate, cache, and retrieve AI-powered news articles."""

    # ── Public API ──────────────────────────────────────────────

    async def generate_for_market(
        self,
        condition_id: str,
        question: str,
        *,
        force: bool = False,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
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

        # get_analysis_client is a synchronous factory returning a pooled
        # client; awaiting it raised TypeError and broke every news generation.
        # News always runs on llm-chain (the only backend implementing
        # GenerateNews); provider/model still take effect via llm_config.
        client = get_analysis_client(backend=_resolve_news_backend(provider))
        # AnalysisClient.generate_news takes market_question (not
        # question) and has no provider/model kwargs — provider and
        # model are forwarded through llm_config instead.
        article = await client.generate_news(
            market_question=question,
            condition_id=condition_id,
            llm_config=_build_news_llm_config(provider, model),
        )

        # Store with 20-min TTL
        news_cache.set(cache_key, article, ttl_seconds=1200)

        # Also append to feed cache
        self._append_to_feed(article)

        return article

    def get_for_market(self, condition_id: str) -> dict[str, Any] | None:
        """Return the cached article for a market, or None."""
        return news_cache.get(f"article:{condition_id}")

    def get_cached_feed(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent cached articles (newest first)."""
        feed: list[dict[str, Any]] = news_cache.get("feed") or []
        return feed[:limit]

    async def generate_for_trending(
        self,
        *,
        max_markets: int = 5,
        provider: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Generate articles for the top trending markets."""
        markets = await self._fetch_trending_markets(max_markets)
        articles: list[dict[str, Any]] = []

        for mkt in markets:
            try:
                article = await self.generate_for_market(
                    condition_id=mkt["condition_id"],
                    question=mkt["question"],
                    force=True,
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

    def _append_to_feed(self, article: dict[str, Any]) -> None:
        """Prepend an article to the global feed cache (capped at 50)."""
        feed: list[dict[str, Any]] = news_cache.get("feed") or []

        # Deduplicate by condition_id
        cid = article.get("condition_id")
        feed = [a for a in feed if a.get("condition_id") != cid]
        feed.insert(0, article)
        feed = feed[:50]

        news_cache.set("feed", feed, ttl_seconds=3600)  # 1-hour feed TTL

    async def _fetch_trending_markets(self, limit: int) -> list[dict[str, Any]]:
        """Get top active markets from Polymarket to generate news for.

        Uses ``get_active_markets``, the method that actually exists on
        ``PolymarketService``. It previously called a non-existent
        ``get_markets(limit, active)``, so the AttributeError was swallowed and
        trending news generation silently produced nothing.
        """
        try:
            from app.services.polymarket_service import get_polymarket_service

            pm = get_polymarket_service()
            markets_raw = await pm.get_active_markets(limit=limit)
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

_news_service: NewsService | None = None


def get_news_service() -> NewsService:
    global _news_service
    if _news_service is None:
        _news_service = NewsService()
    return _news_service


# ── Background generator ────────────────────────────────────


async def _news_generation_loop() -> None:
    """Periodically generate news for trending markets.

    The interval is slept *before* the first cycle so a fresh boot
    does not immediately fire five LLM calls; every later cycle runs
    one ``_GENERATION_INTERVAL`` after the previous one.
    """
    svc = get_news_service()
    while True:
        await asyncio.sleep(_GENERATION_INTERVAL)
        try:
            logger.info("Background news generation: starting cycle")
            articles = await svc.generate_for_trending(max_markets=5)
            logger.info("Background news generation: produced %d articles", len(articles))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Background news generation error: %s", exc)


async def start_news_generator() -> None:
    """Start the periodic news background task.

    A finished (e.g. crashed) task is replaced, so the generator
    can be restarted without an explicit ``stop_news_generator``.
    """
    global _generator_task
    if _generator_task is not None and not _generator_task.done():
        return
    _generator_task = asyncio.create_task(_news_generation_loop())
    logger.info("News background generator started")


async def stop_news_generator() -> None:
    """Cancel the periodic news background task."""
    global _generator_task
    if _generator_task is not None:
        _generator_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _generator_task
        _generator_task = None
        logger.info("News background generator stopped")
