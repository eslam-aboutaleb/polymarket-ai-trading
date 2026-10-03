"""Unit tests for the news service (``app.services.news_service``).

Articles are ephemeral: they live in an in-memory cache with a 20-minute TTL
and are never persisted, so the cache is the source of truth for both the
on-demand API and the feed. These tests therefore assert the exact cache keys,
TTLs and ordering the service writes, not just that it called the LLM.

All external I/O is mocked: the gRPC analysis client (patched as an awaitable
factory, which is what the service awaits) and ``PolymarketService``, which is
imported lazily inside ``_fetch_trending_markets``.

Covered:

* ``generate_for_market`` -- cache hit/miss/force, the ``article:{condition_id}``
  key, the 20-minute TTL, feed insertion, and propagation of gRPC failures
  without polluting the cache.
* ``get_for_market`` / ``get_cached_feed`` -- reads, limits and empty states.
* ``_append_to_feed`` -- newest-first ordering, ``condition_id`` de-duplication
  and the 50-article cap with its one-hour TTL.
* ``generate_for_trending`` / ``_fetch_trending_markets`` -- market
  normalisation, the failure isolation that keeps one bad market from killing
  the cycle, the empty-list fallback, and forced regeneration despite the
  article cache.
* ``_resolve_news_backend`` -- news always routes to llm-chain, whatever
  the provider override.
* ``get_news_service`` and the background generator's start/stop/loop lifecycle.
* ``AnalysisClient`` news contract -- the article dict carries both
  ``question`` and ``market_question``, and the batch RPC parses every
  article and forwards the per-request LLM config.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import AIBackend
from app.services import news_service as news_mod
from app.services.news_service import (
    NewsService,
    get_news_service,
    start_news_generator,
    stop_news_generator,
)

CID = "0xcond-1"


class _RecordingCache:
    """Minimal cache double that records every ``set`` for TTL assertions."""

    def __init__(self):
        self.store: dict = {}
        self.set_calls: list[tuple[str, object, int]] = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_seconds=300):
        self.set_calls.append((key, value, ttl_seconds))
        self.store[key] = value

    def set_calls_for(self, key):
        return [call for call in self.set_calls if call[0] == key]


def _article(condition_id=CID, headline="Headline"):
    return {"condition_id": condition_id, "headline": headline, "summary": "Summary"}


class _NewsServiceTestCase(unittest.IsolatedAsyncioTestCase):
    """Installs a fresh cache double and a fresh client double per test."""

    def setUp(self):
        self.cache = _RecordingCache()
        cache_patcher = patch.object(news_mod, "news_cache", self.cache)
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

        self.client = MagicMock()
        self.client.generate_news = AsyncMock(return_value=_article())
        # get_analysis_client is a *synchronous* factory returning a pooled
        # client. Mocking it as AsyncMock hid a real production bug where the
        # service awaited it and raised TypeError on every call.
        factory_patcher = patch(
            "app.grpc_clients.analysis_client.get_analysis_client",
            MagicMock(return_value=self.client),
        )
        self.factory = factory_patcher.start()
        self.addCleanup(factory_patcher.stop)

        self.service = NewsService()

    async def asyncTearDown(self):
        await stop_news_generator()


class GenerateForMarketTests(_NewsServiceTestCase):
    """Generation, caching and error propagation for a single market."""

    async def test_cache_miss_generates_stores_and_returns_the_article(self):
        article = await self.service.generate_for_market(CID, "Will it rain?")

        self.assertEqual(article["headline"], "Headline")
        self.factory.assert_called_once_with(backend=AIBackend.LLM_CHAIN)
        # Exact-kwargs assertion: AnalysisClient.generate_news takes
        # market_question (not question) and forwards provider/model
        # through llm_config. An AsyncMock alone would accept any
        # kwargs, which is how the signature drift survived.
        self.client.generate_news.assert_awaited_once_with(
            market_question="Will it rain?", condition_id=CID, llm_config=None
        )
        self.assertEqual(
            self.cache.set_calls_for(f"article:{CID}"), [(f"article:{CID}", article, 1200)]
        )

    async def test_provider_and_model_are_forwarded_to_the_client(self):
        await self.service.generate_for_market(
            CID, "Will it rain?", provider="openai", model="gpt-4o"
        )
        self.client.generate_news.assert_awaited_once_with(
            market_question="Will it rain?",
            condition_id=CID,
            llm_config={"provider": 1, "model": "gpt-4o"},
        )

    async def test_github_models_is_routed_to_llm_chain(self):
        # The CLI agent does not implement GenerateNews, so every
        # provider -- github_models included -- runs on llm-chain.
        await self.service.generate_for_market(
            CID, "Will it rain?", provider="github_models", model="gpt-4o"
        )

        self.factory.assert_called_once_with(backend=AIBackend.LLM_CHAIN)
        self.client.generate_news.assert_awaited_once_with(
            market_question="Will it rain?",
            condition_id=CID,
            llm_config={"provider": 6, "model": "gpt-4o"},
        )

    async def test_invalid_provider_yields_no_llm_config(self):
        await self.service.generate_for_market(
            CID, "Will it rain?", provider="not-a-provider", model="gpt-4o"
        )
        self.client.generate_news.assert_awaited_once_with(
            market_question="Will it rain?", condition_id=CID, llm_config=None
        )

    async def test_provider_without_a_model_still_builds_the_config(self):
        await self.service.generate_for_market(CID, "Will it rain?", provider="groq")
        self.client.generate_news.assert_awaited_once_with(
            market_question="Will it rain?", condition_id=CID, llm_config={"provider": 4}
        )

    async def test_cache_hit_short_circuits_the_client(self):
        cached = _article(headline="Cached")
        self.cache.store[f"article:{CID}"] = cached

        result = await self.service.generate_for_market(CID, "Will it rain?")

        self.assertIs(result, cached)
        self.factory.assert_not_called()
        self.client.generate_news.assert_not_awaited()

    async def test_force_regenerates_despite_a_cached_article(self):
        self.cache.store[f"article:{CID}"] = _article(headline="Stale")

        result = await self.service.generate_for_market(CID, "Will it rain?", force=True)

        self.assertEqual(result["headline"], "Headline")
        self.client.generate_news.assert_awaited_once()

    async def test_force_still_writes_the_fresh_article_to_the_cache(self):
        self.cache.store[f"article:{CID}"] = _article(headline="Stale")
        await self.service.generate_for_market(CID, "Will it rain?", force=True)
        self.assertEqual(self.cache.store[f"article:{CID}"]["headline"], "Headline")

    async def test_generated_article_is_also_prepended_to_the_feed(self):
        await self.service.generate_for_market(CID, "Will it rain?")
        feed = self.cache.store["feed"]
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["condition_id"], CID)
        self.assertEqual(self.cache.set_calls_for("feed"), [("feed", feed, 3600)])

    async def test_cache_hit_does_not_duplicate_the_feed_entry(self):
        await self.service.generate_for_market(CID, "Will it rain?")
        # A second, forced generation for the same market must replace the
        # existing feed entry rather than append a duplicate.
        await self.service.generate_for_market(CID, "Will it rain?", force=True)
        self.assertEqual(len(self.cache.store["feed"]), 1)

    async def test_generation_failure_propagates_and_caches_nothing(self):
        self.client.generate_news.side_effect = RuntimeError("gRPC error: UNAVAILABLE")

        with self.assertRaises(RuntimeError) as ctx:
            await self.service.generate_for_market(CID, "Will it rain?")

        self.assertIn("UNAVAILABLE", str(ctx.exception))
        self.assertNotIn(f"article:{CID}", self.cache.store)
        self.assertNotIn("feed", self.cache.store)

    async def test_client_factory_failure_propagates(self):
        self.factory.side_effect = RuntimeError("no analysis backend")

        with self.assertRaises(RuntimeError):
            await self.service.generate_for_market(CID, "Will it rain?")

        self.client.generate_news.assert_not_awaited()


class ResolveNewsBackendTests(unittest.TestCase):
    """News routing: every provider override resolves to llm-chain."""

    def test_every_supported_provider_resolves_to_llm_chain(self):
        for provider in (
            "openai",
            "anthropic",
            "google",
            "groq",
            "ollama",
            "github_models",
        ):
            with self.subTest(provider=provider):
                self.assertIs(news_mod._resolve_news_backend(provider), AIBackend.LLM_CHAIN)

    def test_none_provider_resolves_to_llm_chain(self):
        self.assertIs(news_mod._resolve_news_backend(None), AIBackend.LLM_CHAIN)

    def test_invalid_provider_resolves_to_llm_chain(self):
        self.assertIs(news_mod._resolve_news_backend("not-a-provider"), AIBackend.LLM_CHAIN)


class GetForMarketTests(_NewsServiceTestCase):
    """Read-through of a single cached article."""

    def test_returns_the_cached_article(self):
        article = _article()
        self.cache.store[f"article:{CID}"] = article
        self.assertIs(self.service.get_for_market(CID), article)

    def test_returns_none_for_an_uncached_market(self):
        self.assertIsNone(self.service.get_for_market("0xmissing"))


class CachedFeedTests(_NewsServiceTestCase):
    """Feed reads, ordering, de-duplication and capping."""

    def test_empty_feed_returns_an_empty_list(self):
        self.assertEqual(self.service.get_cached_feed(), [])

    def test_feed_is_returned_newest_first(self):
        for i in range(3):
            self.service._append_to_feed(_article(f"cid-{i}"))
        feed = self.service.get_cached_feed()
        self.assertEqual([a["condition_id"] for a in feed], ["cid-2", "cid-1", "cid-0"])

    def test_limit_truncates_the_feed(self):
        for i in range(5):
            self.service._append_to_feed(_article(f"cid-{i}"))
        self.assertEqual(len(self.service.get_cached_feed(limit=2)), 2)
        self.assertEqual(
            [a["condition_id"] for a in self.service.get_cached_feed(limit=2)],
            ["cid-4", "cid-3"],
        )

    def test_limit_larger_than_the_feed_is_harmless(self):
        self.service._append_to_feed(_article())
        self.assertEqual(len(self.service.get_cached_feed(limit=100)), 1)

    def test_regenerating_a_market_replaces_its_feed_entry(self):
        self.service._append_to_feed(_article("cid-a", headline="Old"))
        self.service._append_to_feed(_article("cid-b"))
        self.service._append_to_feed(_article("cid-a", headline="New"))

        feed = self.service.get_cached_feed()
        self.assertEqual([a["condition_id"] for a in feed], ["cid-a", "cid-b"])
        self.assertEqual(feed[0]["headline"], "New")

    def test_article_without_a_condition_id_still_lands_in_the_feed(self):
        self.service._append_to_feed({"headline": "no id"})
        self.assertEqual(len(self.service.get_cached_feed()), 1)

    def test_feed_is_capped_at_fifty_articles(self):
        for i in range(60):
            self.service._append_to_feed(_article(f"cid-{i}"))
        feed = self.cache.store["feed"]
        self.assertEqual(len(feed), 50)
        self.assertEqual(feed[0]["condition_id"], "cid-59")
        self.assertNotIn({"condition_id": "cid-0"}, feed)

    def test_feed_is_written_with_a_one_hour_ttl(self):
        self.service._append_to_feed(_article())
        self.assertEqual([call[2] for call in self.cache.set_calls_for("feed")], [3600])


class _TrendingMarketCase(_NewsServiceTestCase):
    """Patches the Polymarket service factory for the trending fetch.

    The production code calls ``get_polymarket_service().get_active_markets``;
    an earlier version called a non-existent ``get_markets(limit, active)``,
    which raised AttributeError and was swallowed, so no article was ever
    generated for the trending feed.
    """

    def _patch_markets(self, markets=None, side_effect=None):
        instance = MagicMock()
        instance.get_active_markets = AsyncMock(return_value=markets, side_effect=side_effect)
        factory = MagicMock(return_value=instance)
        patcher = patch("app.services.polymarket_service.get_polymarket_service", factory)
        factory_mock = patcher.start()
        self.addCleanup(patcher.stop)
        self.polymarket = factory_mock
        self.polymarket_instance = instance
        return factory_mock


class FetchTrendingMarketsTests(_TrendingMarketCase):
    """Normalisation of the market list fetched from Polymarket."""

    async def test_markets_are_reduced_to_condition_id_and_question(self):
        self._patch_markets([{"condition_id": "a", "question": "Q A", "extra": "dropped"}])
        markets = await self.service._fetch_trending_markets(5)
        self.assertEqual(markets, [{"condition_id": "a", "question": "Q A"}])
        self.polymarket_instance.get_active_markets.assert_awaited_once_with(limit=5)

    async def test_markets_without_a_condition_id_are_dropped(self):
        self._patch_markets([{"question": "orphan"}, {"condition_id": "a", "question": "Q"}])
        markets = await self.service._fetch_trending_markets(2)
        self.assertEqual(markets, [{"condition_id": "a", "question": "Q"}])

    async def test_missing_question_defaults_to_an_empty_string(self):
        self._patch_markets([{"condition_id": "a"}])
        markets = await self.service._fetch_trending_markets(1)
        self.assertEqual(markets, [{"condition_id": "a", "question": ""}])

    async def test_empty_upstream_response_yields_no_markets(self):
        self._patch_markets([])
        self.assertEqual(await self.service._fetch_trending_markets(5), [])

    async def test_none_upstream_response_yields_no_markets(self):
        self._patch_markets(None)
        self.assertEqual(await self.service._fetch_trending_markets(5), [])

    async def test_upstream_failure_is_logged_and_yields_no_markets(self):
        self._patch_markets(side_effect=RuntimeError("gateway timeout"))

        with self.assertLogs(news_mod.logger, level="WARNING") as captured:
            markets = await self.service._fetch_trending_markets(5)

        self.assertEqual(markets, [])
        self.assertTrue(any("Could not fetch trending markets" in line for line in captured.output))
        self.assertTrue(any("gateway timeout" in line for line in captured.output))


class GenerateForTrendingTests(_TrendingMarketCase):
    """Batch generation over the trending markets."""

    async def test_generates_one_article_per_market_in_order(self):
        self._patch_markets(
            [
                {"condition_id": "a", "question": "Q A"},
                {"condition_id": "b", "question": "Q B"},
            ]
        )
        self.client.generate_news.side_effect = [
            _article("a", "Headline A"),
            _article("b", "Headline B"),
        ]

        articles = await self.service.generate_for_trending(max_markets=2)

        self.assertEqual([a["headline"] for a in articles], ["Headline A", "Headline B"])
        self.polymarket_instance.get_active_markets.assert_awaited_once_with(limit=2)

    async def test_regenerates_despite_cached_articles(self):
        # The refresh-feed endpoint promises regeneration, so the
        # trending cycle must bypass the 20-minute article cache.
        self._patch_markets([{"condition_id": "a", "question": "Q A"}])
        self.cache.store["article:a"] = _article("a", headline="Stale")

        articles = await self.service.generate_for_trending(max_markets=1)

        self.assertEqual([a["headline"] for a in articles], ["Headline"])
        self.assertEqual(self.cache.store["article:a"]["headline"], "Headline")

    async def test_provider_and_model_are_forwarded_to_every_generation(self):
        self._patch_markets([{"condition_id": "a", "question": "Q A"}])

        await self.service.generate_for_trending(provider="openai", model="gpt-4o")

        self.client.generate_news.assert_awaited_once_with(
            market_question="Q A",
            condition_id="a",
            llm_config={"provider": 1, "model": "gpt-4o"},
        )

    async def test_a_failing_market_is_skipped_and_the_cycle_continues(self):
        self._patch_markets(
            [
                {"condition_id": "a", "question": "Q A"},
                {"condition_id": "b", "question": "Q B"},
            ]
        )
        self.client.generate_news.side_effect = [
            RuntimeError("gRPC error: DEADLINE_EXCEEDED"),
            _article("b", "Headline B"),
        ]

        with self.assertLogs(news_mod.logger, level="WARNING") as captured:
            articles = await self.service.generate_for_trending()

        self.assertEqual([a["condition_id"] for a in articles], ["b"])
        self.assertTrue(any("Failed to generate news for Q A" in line for line in captured.output))

    async def test_no_trending_markets_produces_no_articles(self):
        self._patch_markets([])
        self.assertEqual(await self.service.generate_for_trending(), [])
        self.factory.assert_not_called()

    async def test_default_batch_size_is_five_markets(self):
        self._patch_markets([])
        await self.service.generate_for_trending()
        self.polymarket_instance.get_active_markets.assert_awaited_once_with(limit=5)


class GetNewsServiceTests(unittest.TestCase):
    """Module-level singleton."""

    def tearDown(self):
        news_mod._news_service = None

    def test_first_call_creates_and_second_call_reuses_the_service(self):
        news_mod._news_service = None
        first = get_news_service()
        second = get_news_service()
        self.assertIsInstance(first, NewsService)
        self.assertIs(first, second)

    def test_existing_instance_is_returned_without_rebuilding(self):
        sentinel = object()
        news_mod._news_service = sentinel
        self.assertIs(get_news_service(), sentinel)

    def test_constructing_the_class_directly_does_not_touch_the_singleton(self):
        news_mod._news_service = None
        NewsService()
        self.assertIsNone(news_mod._news_service)


class BackgroundGeneratorTests(_NewsServiceTestCase):
    """The periodic generator task and its loop."""

    async def test_start_creates_the_generator_task(self):
        await start_news_generator()
        self.assertIsInstance(news_mod._generator_task, asyncio.Task)
        self.assertEqual(news_mod._generator_task.get_coro().__name__, "_news_generation_loop")

    async def test_second_start_while_running_keeps_the_same_task(self):
        await start_news_generator()
        first = news_mod._generator_task
        await start_news_generator()
        self.assertIs(news_mod._generator_task, first)

    async def test_stop_cancels_the_task_and_clears_the_handle(self):
        await start_news_generator()
        task = news_mod._generator_task
        await stop_news_generator()
        self.assertIsNone(news_mod._generator_task)
        self.assertTrue(task.cancelled() or task.done())

    async def test_stop_without_a_task_is_a_no_op(self):
        news_mod._generator_task = None
        await stop_news_generator()
        self.assertIsNone(news_mod._generator_task)

    async def test_stop_after_the_task_finished_still_clears_the_handle(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        news_mod._generator_task = finished

        await stop_news_generator()

        self.assertIsNone(news_mod._generator_task)
        self.assertFalse(finished.cancelled())
        self.assertTrue(finished.done())

    async def test_start_replaces_a_finished_task(self):
        # A crashed (finished) generator is replaced on restart: the
        # guard checks .done(), not just a non-None handle.
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        news_mod._generator_task = finished

        await start_news_generator()

        self.assertIsNot(news_mod._generator_task, finished)
        self.assertFalse(news_mod._generator_task.done())

    async def test_start_announces_itself(self):
        with (
            patch.object(news_mod, "get_news_service"),
            self.assertLogs(news_mod.logger, level="INFO") as captured,
        ):
            await start_news_generator()
        self.assertTrue(
            any("News background generator started" in line for line in captured.output)
        )

    async def test_stop_announces_itself(self):
        await start_news_generator()
        with self.assertLogs(news_mod.logger, level="INFO") as captured:
            await stop_news_generator()
        self.assertTrue(
            any("News background generator stopped" in line for line in captured.output)
        )


class NewsGenerationLoopTests(_NewsServiceTestCase):
    """One pass of the periodic loop, driven to completion deterministically."""

    def _patch_service(self, generate_for_trending):
        service = MagicMock()
        service.generate_for_trending = generate_for_trending
        patcher = patch.object(news_mod, "get_news_service", return_value=service)
        patcher.start()
        self.addCleanup(patcher.stop)
        return service

    async def test_sleeps_before_the_first_cycle_then_generates(self):
        # The interval is slept *before* the first cycle so a
        # fresh boot does not immediately fire five LLM calls.
        generate = AsyncMock(return_value=[_article("a"), _article("b")])
        self._patch_service(generate)
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError("stop")])

        with (
            self.assertLogs(news_mod.logger, level="INFO") as captured,
            patch.object(asyncio, "sleep", sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await news_mod._news_generation_loop()

        generate.assert_awaited_once_with(max_markets=5)
        self.assertEqual(sleep.await_count, 2)
        sleep.assert_any_await(news_mod._GENERATION_INTERVAL)
        self.assertEqual(news_mod._GENERATION_INTERVAL, 600)
        self.assertTrue(any("starting cycle" in line for line in captured.output))
        self.assertTrue(any("produced 2 articles" in line for line in captured.output))

    async def test_generation_failure_is_logged_and_the_loop_keeps_running(self):
        generate = AsyncMock(side_effect=RuntimeError("all backends down"))
        self._patch_service(generate)
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError("stop")])

        with (
            self.assertLogs(news_mod.logger, level="ERROR") as captured,
            patch.object(asyncio, "sleep", sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await news_mod._news_generation_loop()

        self.assertEqual(generate.await_count, 2)
        self.assertTrue(any("Background news generation error" in line for line in captured.output))

    async def test_zero_articles_is_logged_without_an_error(self):
        self._patch_service(AsyncMock(return_value=[]))
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError("stop")])

        with (
            self.assertLogs(news_mod.logger, level="INFO") as captured,
            patch.object(asyncio, "sleep", sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await news_mod._news_generation_loop()

        self.assertTrue(any("produced 0 articles" in line for line in captured.output))

    async def test_cancellation_during_generation_propagates_without_another_sleep(self):
        # Cancellation must not be swallowed and logged as a cycle error, and
        # the loop must not linger for another ten minutes before exiting.
        self._patch_service(AsyncMock(side_effect=asyncio.CancelledError))
        sleep = AsyncMock()

        with patch.object(asyncio, "sleep", sleep), self.assertRaises(asyncio.CancelledError):
            await news_mod._news_generation_loop()

        # Only the pre-cycle sleep ran; no post-cycle sleep before exiting.
        self.assertEqual(sleep.await_count, 1)


class AnalysisClientNewsContractTests(unittest.IsolatedAsyncioTestCase):
    """AnalysisClient news RPC contract (article dict shape)."""

    def _news_response(self, **overrides):
        fields: dict[str, object] = {
            "success": True,
            "headline": "Headline",
            "summary": "Summary",
            "body": "Body",
            "sentiment": "bullish",
            "confidence": 0.9,
            "key_insights": ["insight"],
            "trader_behavior_summary": "trader behavior",
            "market_outlook": "outlook",
            "tags": ["tag"],
            "market_question": "Will it rain?",
            "condition_id": CID,
            "generated_at": "2026-01-01T00:00:00Z",
            "metadata": {"k": "v"},
        }
        fields.update(overrides)
        return MagicMock(**fields)

    async def test_generate_news_returns_both_question_keys(self):
        from app.grpc_clients.analysis_client import AnalysisClient

        stub = MagicMock()
        stub.GenerateNews = AsyncMock(return_value=self._news_response())
        client = AnalysisClient()

        with patch.object(AnalysisClient, "_get_stub", AsyncMock(return_value=stub)):
            article = await client.generate_news(market_question="Will it rain?", condition_id=CID)

        self.assertEqual(article["question"], "Will it rain?")
        self.assertEqual(article["market_question"], "Will it rain?")
        self.assertEqual(article["condition_id"], CID)

    async def test_generate_news_batch_parses_each_article(self):
        from app.grpc_clients.analysis_client import AnalysisClient

        stub = MagicMock()
        stub.GenerateNewsBatch = AsyncMock(
            return_value=MagicMock(
                success=True,
                articles=[
                    self._news_response(condition_id="a", market_question="Q A"),
                    self._news_response(condition_id="b", market_question="Q B"),
                ],
            )
        )
        client = AnalysisClient()

        with patch.object(AnalysisClient, "_get_stub", AsyncMock(return_value=stub)):
            articles = await client.generate_news_batch(
                markets=[
                    {"question": "Q A", "condition_id": "a"},
                    {"question": "Q B", "condition_id": "b"},
                ],
                llm_config={"provider": 1, "model": "gpt-4o"},
            )

        self.assertEqual([a["question"] for a in articles], ["Q A", "Q B"])
        self.assertEqual([a["market_question"] for a in articles], ["Q A", "Q B"])

        request = stub.GenerateNewsBatch.await_args.args[0]
        self.assertEqual(len(request.markets), 2)
        self.assertEqual(request.markets[0].market_question, "Q A")
        self.assertEqual(request.markets[0].condition_id, "a")
        self.assertEqual(request.markets[0].llm_config.provider, 1)
        self.assertEqual(request.markets[0].llm_config.model, "gpt-4o")


if __name__ == "__main__":
    unittest.main()
