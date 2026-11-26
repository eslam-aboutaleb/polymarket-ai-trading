"""Live CLOB order-book feed: real recorded frames, real websocket, real Gamma payloads.

Nothing here mocks the feed logic:

* ``polymarket_clob_ws_btc5m_sample.json.gz`` holds the first 500 raw frames the
  Polymarket market websocket sent for a live BTC 5m market (``book`` snapshots
  and ``price_change`` deltas, recorded Nov 26 2026). The tests replay them
  through the real ``LiveBookFeed`` and check the reconstructed best bid/ask
  against the best bid/ask the exchange states inside the frames themselves.
* The connection test runs the real ``websockets`` client against a local
  websocket server that speaks the market-channel protocol (subscribe frame,
  unsubscribe frame, PING, snapshot + deltas, dropped connection).
* ``polymarket_gamma_updown_5m_events.json`` is the real Gamma payload for the
  same BTC and ETH 5m windows; it pins the discovery fix.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import websockets

from app.services.market_data import crypto_markets_service as cms
from app.services.market_data import live_book_service as lbs
from app.services.market_data.live_book_service import LiveBookFeed

FIXTURES = Path(__file__).parent / "fixtures"


def _load_ws_sample() -> dict:
    with gzip.open(FIXTURES / "polymarket_clob_ws_btc5m_sample.json.gz", "rt") as fh:
        return json.load(fh)


def _load_gamma_events() -> dict:
    return json.loads((FIXTURES / "polymarket_gamma_updown_5m_events.json").read_text())


def _feed_with_sample(max_age_s: float = 3.0) -> tuple[LiveBookFeed, dict]:
    sample = _load_ws_sample()
    feed = LiveBookFeed(max_age_s=max_age_s)
    feed._connected = True  # connection handling is covered by the socket tests
    feed.set_tokens(sample["tokens"])
    for received_at, frame in sample["messages"]:
        feed.handle_message(frame, now=received_at)
    return feed, sample


class RecordedFramesTests(unittest.TestCase):
    def test_reconstructed_best_prices_match_the_exchange_stated_best_prices(self):
        feed, _ = _feed_with_sample()
        stats = feed.stats()
        self.assertGreater(stats["checked_changes"], 500)
        self.assertEqual(stats["best_mismatches"], 0)
        self.assertEqual(stats["tokens_with_book"], 2)

    def test_quotes_are_two_sided_and_up_down_are_complements(self):
        feed, sample = _feed_with_sample()
        now = sample["messages"][-1][0] + 0.5
        up = feed.quote(sample["tokens"][0], now=now)
        down = feed.quote(sample["tokens"][1], now=now)
        self.assertIsNotNone(up)
        self.assertIsNotNone(down)
        self.assertLess(up.best_bid, up.best_ask)
        self.assertLess(down.best_bid, down.best_ask)
        # Buying Up at the ask is the mirror of selling Down at the bid.
        self.assertAlmostEqual(up.best_ask + down.best_bid, 1.0, delta=0.011)
        self.assertAlmostEqual(up.best_bid + down.best_ask, 1.0, delta=0.011)
        self.assertEqual(len(up.bids), 5)
        self.assertEqual([p for p, _ in up.asks], sorted(p for p, _ in up.asks))
        self.assertLessEqual(up.last_event_age_s, 0.6)

    def test_quote_goes_stale_when_no_frames_arrive(self):
        feed, sample = _feed_with_sample(max_age_s=3.0)
        last = sample["messages"][-1][0]
        self.assertIsNotNone(feed.quote(sample["tokens"][0], now=last + 2.9))
        self.assertIsNone(feed.quote(sample["tokens"][0], now=last + 3.1))

    def test_no_quote_while_disconnected(self):
        feed, sample = _feed_with_sample()
        feed._connected = False
        self.assertIsNone(feed.quote(sample["tokens"][0], now=sample["messages"][-1][0]))

    def test_delta_without_snapshot_is_ignored(self):
        sample = _load_ws_sample()
        feed = LiveBookFeed()
        feed._connected = True
        deltas = [m for _, m in sample["messages"] if '"price_changes"' in m][:20]
        for frame in deltas:
            feed.handle_message(frame, now=1.0)
        self.assertEqual(feed.stats()["tokens_with_book"], 0)
        self.assertIsNone(feed.quote(sample["tokens"][0], now=1.0))

    def test_size_zero_removes_a_level_and_crossed_book_is_refused(self):
        token = "111"
        feed = LiveBookFeed()
        feed._connected = True
        feed.handle_message(
            {
                "event_type": "book",
                "asset_id": token,
                "bids": [{"price": "0.40", "size": "10"}, {"price": "0.39", "size": "5"}],
                "asks": [{"price": "0.42", "size": "7"}],
            },
            now=1.0,
        )
        change = {"asset_id": token, "price": "0.40", "size": "0", "side": "BUY"}
        feed.handle_message({"event_type": "price_change", "price_changes": [change]}, now=1.1)
        quote = feed.quote(token, now=1.2)
        self.assertEqual(quote.best_bid, 0.39)
        crossed = {"asset_id": token, "price": "0.45", "size": "3", "side": "BUY"}
        feed.handle_message({"event_type": "price_change", "price_changes": [crossed]}, now=1.3)
        self.assertIsNone(feed.quote(token, now=1.4))

    def test_pong_refreshes_liveness_but_not_the_book(self):
        feed, sample = _feed_with_sample(max_age_s=3.0)
        last = sample["messages"][-1][0]
        feed.handle_message("PONG", now=last + 5.0)
        quote = feed.quote(sample["tokens"][0], now=last + 5.5)
        self.assertIsNotNone(quote)
        self.assertGreater(quote.last_event_age_s, 5.0)


class WebsocketSessionTests(unittest.IsolatedAsyncioTestCase):
    """The real client against a local server speaking the market-channel protocol."""

    async def asyncSetUp(self):
        self.sample = _load_ws_sample()
        self.received: list[str] = []
        self.connections = 0
        self.drop_next = asyncio.Event()

    async def _handler(self, ws):
        self.connections += 1
        first_connection = self.connections == 1
        frames = [m for _, m in self.sample["messages"][:40]]

        async def reader():
            async for raw in ws:
                self.received.append(raw)

        read_task = asyncio.create_task(reader())
        try:
            while not any('"market"' in r for r in self.received):
                await asyncio.sleep(0.01)
            for frame in frames:
                await ws.send(frame)
            drop_task = asyncio.create_task(self.drop_next.wait())
            if first_connection:
                # Hold the socket open until the test asks to drop it (or the client leaves).
                await asyncio.wait({drop_task, read_task}, return_when=asyncio.FIRST_COMPLETED)
                if drop_task.done():
                    await ws.close()
            else:
                await asyncio.wait({read_task}, timeout=5)
            drop_task.cancel()
        finally:
            read_task.cancel()

    async def _wait_for(self, predicate, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False

    async def test_subscribe_quote_unsubscribe_and_reconnect(self):
        async with websockets.serve(self._handler, "127.0.0.1", 0, max_size=None) as server:
            port = server.sockets[0].getsockname()[1]
            feed = LiveBookFeed(url=f"ws://127.0.0.1:{port}", max_age_s=5.0)
            tokens = self.sample["tokens"]
            feed.set_tokens(tokens)
            with patch.object(lbs, "INITIAL_RECONNECT_DELAY_SECONDS", 0.05):
                await feed.start()
                try:
                    got_quote = await self._wait_for(lambda: feed.quote(tokens[0]) is not None)
                    self.assertTrue(got_quote, feed.stats())
                    subscribe = json.loads(self.received[0])
                    self.assertEqual(subscribe["type"], "market")
                    self.assertEqual(sorted(subscribe["assets_ids"]), sorted(tokens))
                    self.assertEqual(feed.stats()["best_mismatches"], 0)

                    # Dropping a token sends an unsubscribe and discards its book.
                    feed.set_tokens([tokens[0]])
                    saw_unsub = await self._wait_for(
                        lambda: any('"unsubscribe"' in r for r in self.received)
                    )
                    self.assertTrue(saw_unsub)
                    unsub = json.loads(next(r for r in self.received if '"unsubscribe"' in r))
                    self.assertEqual(unsub["assets_ids"], [tokens[1]])
                    self.assertIsNone(feed.quote(tokens[1]))

                    # Server drops the socket: no quote while down, then the feed
                    # reconnects, re-subscribes and rebuilds the book from a new snapshot.
                    self.drop_next.set()
                    went_down = await self._wait_for(lambda: not feed.connected)
                    self.assertTrue(went_down)
                    self.assertIsNone(feed.quote(tokens[0]))
                    back = await self._wait_for(
                        lambda: self.connections >= 2 and feed.quote(tokens[0]) is not None
                    )
                    self.assertTrue(back, feed.stats())
                    self.assertGreaterEqual(feed.stats()["reconnects"], 1)
                    resub = [json.loads(r) for r in self.received if '"market"' in r]
                    self.assertGreaterEqual(len(resub), 2)
                    self.assertEqual(resub[-1]["assets_ids"], [tokens[0]])
                finally:
                    await feed.stop()
        self.assertFalse(feed.connected)

    async def test_ping_heartbeat_is_sent(self):
        async with websockets.serve(self._handler, "127.0.0.1", 0, max_size=None) as server:
            port = server.sockets[0].getsockname()[1]
            feed = LiveBookFeed(url=f"ws://127.0.0.1:{port}")
            feed.set_tokens(self.sample["tokens"])
            with patch.object(lbs, "HEARTBEAT_INTERVAL_SECONDS", 0.1):
                await feed.start()
                try:
                    pinged = await self._wait_for(lambda: "PING" in self.received, timeout=3.0)
                    self.assertTrue(pinged, self.received)
                finally:
                    await feed.stop()


class DiscoveryWithRealGammaPayloadTests(unittest.TestCase):
    """Regression: the old parser found 0 markets in today's Gamma format."""

    def setUp(self):
        self.events = list(_load_gamma_events().values())
        self.now = datetime(2026, 11, 25, 23, 55, 20, tzinfo=UTC)
        self.window_start = 1795650900  # 2026-11-25T23:55:00Z

    def test_btc_and_eth_5m_windows_are_found(self):
        markets = cms.parse_crypto_markets(self.events, self.now)
        self.assertIn(("BTC", 5, self.window_start), markets)
        self.assertIn(("ETH", 5, self.window_start), markets)

    def test_tokens_come_from_clob_token_ids_and_outcome_labels(self):
        markets = cms.parse_crypto_markets(self.events, self.now)
        btc = markets[("BTC", 5, self.window_start)]
        event = self.events[0]["markets"][0]
        ids = json.loads(event["clobTokenIds"])
        self.assertEqual(btc.up_token_id, ids[0])
        self.assertEqual(btc.down_token_id, ids[1])
        self.assertEqual(btc.condition_id, event["conditionId"])
        self.assertAlmostEqual(btc.up_price, 0.485)

    def test_recorded_ws_tokens_belong_to_the_discovered_btc_market(self):
        markets = cms.parse_crypto_markets(self.events, self.now)
        btc = markets[("BTC", 5, self.window_start)]
        self.assertEqual([btc.up_token_id, btc.down_token_id], _load_ws_sample()["tokens"])

    def test_window_start_is_the_event_start_not_the_creation_date(self):
        markets = cms.parse_crypto_markets(self.events, self.now)
        btc = markets[("BTC", 5, self.window_start)]
        self.assertEqual(int(btc.start_date.timestamp()), self.window_start)
        self.assertTrue(cms.window_start_matches(btc, 5, self.now))

    def test_slug_list_names_current_and_next_windows(self):
        slugs = cms.window_slugs(self.now)
        self.assertIn(f"btc-updown-5m-{self.window_start}", slugs)
        self.assertIn(f"eth-updown-5m-{self.window_start + 300}", slugs)
        self.assertIn(f"sol-updown-15m-{1795650300}", slugs)  # 23:45 boundary
        self.assertEqual(len(slugs), len(set(slugs)))


class EngineUsesLiveBookTests(unittest.TestCase):
    """The edge is priced from the live book, and skipped when the book is not fresh."""

    def setUp(self):
        from app.services.trading import latency_arb_service as svc

        self.svc = svc
        self.now = datetime(2026, 11, 25, 23, 56, 0, tzinfo=UTC)
        self.window_start = 1795650900
        events = list(_load_gamma_events().values())
        self.markets = cms.parse_crypto_markets(events, self.now)
        self.sample = _load_ws_sample()
        self.clock_now = [self.sample["messages"][-1][0] + 0.2]
        feed = LiveBookFeed(max_age_s=3.0, clock=lambda: self.clock_now[0])
        feed._connected = True
        feed.set_tokens(self.sample["tokens"])
        for received_at, frame in self.sample["messages"]:
            feed.handle_message(frame, now=received_at)
        self.feed = feed
        self._patches = [
            patch.object(lbs, "_feed", feed),
            patch.object(cms, "_markets", self.markets),
            patch.object(svc.binance_ws_service, "get_last_price", lambda s: 60000.0),
            patch.object(svc.binance_ws_service, "get_window_open_price", lambda s, e: 59950.0),
            patch.object(
                svc.binance_ws_service,
                "get_recent_closes",
                lambda s: [59900.0 + i for i in range(30)],
            ),
            patch.object(svc.binance_ws_service, "get_feed_lag_ms", lambda s: 50.0),
            # Pin the wall clock the market lookup uses to the recorded window.
            patch.object(cms, "current_window_start", lambda w, now=None: 1795650900),
            patch.object(svc, "_engine_symbols", lambda: ["BTC"]),
            patch.object(svc, "_engine_windows", lambda: [5]),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _run(self):
        seen = []
        real = self.svc.evaluate_opportunity

        def spy(symbol, window, market, *args, **kwargs):
            seen.append(market)
            return real(symbol, window, market, *args, **kwargs)

        with patch.object(self.svc, "evaluate_opportunity", spy):
            result = asyncio.run(self.svc._fetch_opportunities(self.now))
        return result, seen

    def test_market_price_is_the_live_book_mid_not_the_gamma_price(self):
        _, seen = self._run()
        self.assertEqual(len(seen), 1)
        btc = self.markets[("BTC", 5, self.window_start)]
        up = self.feed.quote(btc.up_token_id)
        self.assertAlmostEqual(seen[0].up_price, up.mid)
        self.assertNotAlmostEqual(seen[0].up_price, btc.up_price, places=3)  # gamma said 0.485
        self.assertEqual(seen[0].up_token_id, btc.up_token_id)

    def test_stale_book_means_no_evaluation_and_no_gamma_fallback(self):
        self.clock_now[0] += 10.0  # no frames for 10 s
        before = self.svc._book_feed_skips[0]
        result, seen = self._run()
        self.assertEqual(seen, [])
        self.assertEqual(result, [])
        self.assertEqual(self.svc._book_feed_skips[0], before + 1)

    def test_disabled_flag_keeps_the_old_gamma_price_path(self):
        settings = self.svc.get_settings().model_copy(
            update={"latency_arb_book_feed_enabled": False}
        )
        with patch.object(self.svc, "get_settings", lambda: settings):
            _, seen = self._run()
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0].up_price, 0.485)

    def test_status_reports_feed_health(self):
        status = self.svc.get_engine_status()
        self.assertTrue(status["book_feed"]["enabled"])
        self.assertEqual(status["book_feed"]["tokens_with_book"], 2)
        self.assertIn("skipped_no_fresh_book", status["book_feed"])


class EngineLifecycleWiresFeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_start_and_stop_run_the_book_feed(self):
        from app.services.trading import latency_arb_service as svc

        feed = LiveBookFeed(url="ws://127.0.0.1:1")  # nothing listens: exercises backoff
        settings = svc.get_settings().model_copy(update={"latency_arb_book_feed_enabled": True})
        with (
            patch.object(lbs, "_feed", feed),
            patch.object(svc, "get_settings", lambda: settings),
            patch.object(svc, "acquire_scheduler_lock", lambda name: True),
            patch.object(svc, "release_scheduler_lock", lambda name: None),
            patch.object(svc, "_engine_cycle", lambda: asyncio.sleep(0)),
            patch.object(svc.binance_ws_service, "start_binance_feed", lambda: asyncio.sleep(0)),
            patch.object(svc.binance_ws_service, "stop_binance_feed", lambda: asyncio.sleep(0)),
            patch.object(
                svc.crypto_markets_service, "start_crypto_markets_refresh", lambda: asyncio.sleep(0)
            ),
            patch.object(
                svc.crypto_markets_service, "stop_crypto_markets_refresh", lambda: asyncio.sleep(0)
            ),
        ):
            await svc.start_latency_arb_engine()
            try:
                self.assertIsNotNone(feed._task)
                self.assertFalse(feed._task.done())
            finally:
                await svc.stop_latency_arb_engine()
        self.assertIsNone(feed._task)
        self.assertFalse(feed.connected)


class AiContextQuoteTests(unittest.IsolatedAsyncioTestCase):
    """The read-only quote the AI agent sees comes from the same feed."""

    def setUp(self):
        now = datetime(2026, 11, 25, 23, 56, 0, tzinfo=UTC)
        self.markets = cms.parse_crypto_markets(list(_load_gamma_events().values()), now)
        self.sample = _load_ws_sample()
        self.clock_now = [self.sample["messages"][-1][0] + 0.2]
        feed = LiveBookFeed(max_age_s=3.0, clock=lambda: self.clock_now[0])
        feed._connected = True
        feed.set_tokens(self.sample["tokens"])
        for received_at, frame in self.sample["messages"]:
            feed.handle_message(frame, now=received_at)
        self.feed = feed
        self.btc = self.markets[("BTC", 5, 1795650900)]
        self._patches = [
            patch.object(lbs, "_feed", feed),
            patch.object(cms, "_markets", self.markets),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    async def test_route_returns_live_quote_matching_the_feed(self):
        from app.api.routes.analysis.analysis import market_live_quote

        body = await market_live_quote(self.btc.condition_id)
        up = self.feed.quote(self.btc.up_token_id)
        self.assertEqual(body["source"], "clob_ws")
        self.assertEqual(body["up"]["best_bid"], up.best_bid)
        self.assertEqual(body["up"]["best_ask"], up.best_ask)
        self.assertEqual(body["symbol"], "BTC")
        self.assertLess(body["up"]["feed_age_s"], 1.0)

    async def test_route_404s_when_book_is_stale_or_market_unknown(self):
        from fastapi import HTTPException

        from app.api.routes.analysis.analysis import market_live_quote

        with self.assertRaises(HTTPException) as unknown:
            await market_live_quote("0x" + "ab" * 32)
        self.assertEqual(unknown.exception.status_code, 404)
        self.clock_now[0] += 30.0
        with self.assertRaises(HTTPException) as stale:
            await market_live_quote(self.btc.condition_id)
        self.assertEqual(stale.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
