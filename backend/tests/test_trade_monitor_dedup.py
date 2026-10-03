"""
Cursor-seeding and dedup tests for ``trade_monitor``.

The monitor detects trades over two channels: an HTTP poll
(``_detect_new_trades``) and WebSocket events that go straight
to ``_process_new_trade``.  Without a seeded cursor the first
poll replays up to ``limit`` historical trades as new, and
without a dedup set the poll re-delivers every trade the WS
already handled — producing duplicate trade_history rows and
duplicate copy orders (no unique constraint catches it).
"""

import contextlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.trade_monitor as trade_monitor


class _MonitorStateTestCase(unittest.IsolatedAsyncioTestCase):
    """Reset the module-level monitor state around each test."""

    def setUp(self):
        trade_monitor._last_seen_trades = {}
        trade_monitor._processed_trade_ids = {}

    async def asyncTearDown(self):
        trade_monitor._last_seen_trades = {}
        trade_monitor._processed_trade_ids = {}


@contextlib.contextmanager
def _pipeline_doubles():
    """Replace the DB-touching collaborators of ``_process_new_trade``.

    Yields the session double plus the patched collaborators so
    tests can assert on how far a trade got through the pipeline.
    """
    session = MagicMock(name="db-session")
    session.query.return_value.filter.return_value.all.return_value = []
    ensure_market = MagicMock(name="_ensure_market")
    position_state = MagicMock(name="_update_trader_position_state")
    position_state.return_value = ("opened", 0.0, 10.0)
    notifications = AsyncMock(name="_create_follow_notifications")
    copy_trade = AsyncMock(name="_trigger_copy_trade")
    with (
        patch.object(trade_monitor, "SessionLocal", return_value=session),
        patch.object(trade_monitor, "_ensure_market", ensure_market),
        patch.object(trade_monitor, "_update_trader_position_state", position_state),
        patch.object(trade_monitor, "_create_follow_notifications", notifications),
        patch.object(trade_monitor, "_trigger_copy_trade", copy_trade),
    ):
        yield session, ensure_market, position_state, notifications, copy_trade


class CursorSeedingTests(_MonitorStateTestCase):
    """The first observation seeds the cursor and skips history."""

    async def test_first_observation_seeds_cursor_and_skips_history(self):
        trades = [{"id": "t3"}, {"id": "t2"}, {"id": "t1"}]

        result = await trade_monitor._detect_new_trades("0xabc", trades)

        self.assertEqual(result, [])
        self.assertEqual(trade_monitor._last_seen_trades["0xabc"], "t3")

    async def test_second_poll_with_same_trades_returns_nothing(self):
        trades = [{"id": "t3"}, {"id": "t2"}]
        await trade_monitor._detect_new_trades("0xabc", trades)

        result = await trade_monitor._detect_new_trades("0xabc", trades)

        self.assertEqual(result, [])

    async def test_only_trades_newer_than_the_cursor_are_returned(self):
        await trade_monitor._detect_new_trades("0xabc", [{"id": "t2"}, {"id": "t1"}])
        newer = [{"id": "t4"}, {"id": "t2"}, {"id": "t1"}]

        result = await trade_monitor._detect_new_trades("0xabc", newer)

        self.assertEqual(result, [{"id": "t4"}])


class ProcessNewTradeDedupTests(_MonitorStateTestCase):
    """A trade already processed once is not processed again."""

    async def test_repeated_trade_id_runs_the_pipeline_once(self):
        trade = {
            "id": "trade-1",
            "market": "mkt-1",
            "side": "BUY",
            "size": 10,
            "price": 0.5,
        }

        with _pipeline_doubles() as (_session, ensure_market, *_rest):
            await trade_monitor._process_new_trade("0xabc", trade)
            await trade_monitor._process_new_trade("0xabc", trade)

        ensure_market.assert_called_once()

    async def test_ws_trade_id_is_deduped_against_poll_delivery(self):
        poll_row = {
            "id": "trade-9",
            "market": "mkt-1",
            "side": "SELL",
            "size": 4,
            "price": 0.6,
        }
        ws_event = {
            "trade_id": "trade-9",
            "market": "mkt-1",
            "side": "SELL",
            "size": 4,
            "price": 0.6,
        }

        with _pipeline_doubles() as (session, ensure_market, *_rest):
            await trade_monitor._process_new_trade("0xabc", poll_row)
            await trade_monitor._process_new_trade("0xabc", ws_event)

        ensure_market.assert_called_once()
        # The WS-sourced row still carries the external trade id.
        recorded = session.add.call_args.args[0]
        self.assertEqual(recorded.source_trade_id_ext, "trade-9")

    async def test_id_less_events_are_deduped_by_fingerprint(self):
        event = {
            "market": "mkt-1",
            "side": "BUY",
            "size": 7,
            "price": 0.4,
            "match_time": "2026-01-01T00:00:00Z",
        }

        with _pipeline_doubles() as (_session, ensure_market, *_rest):
            await trade_monitor._process_new_trade("0xabc", event)
            await trade_monitor._process_new_trade("0xabc", dict(event))

        ensure_market.assert_called_once()

    async def test_distinct_trades_are_both_processed(self):
        first = {
            "id": "trade-a",
            "market": "mkt-1",
            "side": "BUY",
            "size": 1,
            "price": 0.5,
        }
        second = {
            "id": "trade-b",
            "market": "mkt-1",
            "side": "BUY",
            "size": 1,
            "price": 0.5,
        }

        with _pipeline_doubles() as (_session, ensure_market, *_rest):
            await trade_monitor._process_new_trade("0xabc", first)
            await trade_monitor._process_new_trade("0xabc", second)

        self.assertEqual(ensure_market.call_count, 2)

    async def test_dedup_is_tracked_per_wallet(self):
        trade = {
            "id": "trade-1",
            "market": "mkt-1",
            "side": "BUY",
            "size": 10,
            "price": 0.5,
        }

        with _pipeline_doubles() as (_session, ensure_market, *_rest):
            await trade_monitor._process_new_trade("0xaaa", trade)
            await trade_monitor._process_new_trade("0xbbb", trade)

        self.assertEqual(ensure_market.call_count, 2)


class DedupKeyTests(unittest.TestCase):
    """The dedup key prefers exchange ids, then fingerprints."""

    def test_prefers_id_over_trade_id(self):
        self.assertEqual(trade_monitor._trade_dedup_key({"id": "a"}), "a")
        self.assertEqual(trade_monitor._trade_dedup_key({"trade_id": "b"}), "b")
        self.assertEqual(
            trade_monitor._trade_dedup_key({"id": "a", "trade_id": "b"}),
            "a",
        )

    def test_fingerprints_events_without_any_id(self):
        key = trade_monitor._trade_dedup_key(
            {
                "market": "m",
                "side": "BUY",
                "size": 5,
                "price": 0.5,
                "match_time": "t",
            }
        )
        self.assertEqual(key, "fp:m:BUY:5:0.5:t")

    def test_returns_none_when_unidentifiable(self):
        self.assertIsNone(trade_monitor._trade_dedup_key({}))
        self.assertIsNone(trade_monitor._trade_dedup_key({"market": "m"}))


class DedupBoundTests(unittest.TestCase):
    """The per-wallet processed-id set is a bounded LRU."""

    def setUp(self):
        trade_monitor._processed_trade_ids = {}

    def tearDown(self):
        trade_monitor._processed_trade_ids = {}

    def test_oldest_entries_are_evicted_past_the_limit(self):
        wallet = "0xabc"
        for i in range(trade_monitor._PROCESSED_IDS_MAX + 1):
            trade_monitor._already_processed(wallet, f"key-{i}")

        seen = trade_monitor._processed_trade_ids[wallet]

        self.assertNotIn("key-0", seen)
        self.assertIn(f"key-{trade_monitor._PROCESSED_IDS_MAX}", seen)
        self.assertEqual(len(seen), trade_monitor._PROCESSED_IDS_MAX)

    def test_recently_used_key_survives_eviction(self):
        wallet = "0xabc"
        for i in range(trade_monitor._PROCESSED_IDS_MAX + 1):
            trade_monitor._already_processed(wallet, f"key-{i}")

        # Touch the oldest surviving key, then insert one more.
        self.assertTrue(trade_monitor._already_processed(wallet, "key-1"))
        trade_monitor._already_processed(wallet, "key-new")

        seen = trade_monitor._processed_trade_ids[wallet]

        self.assertIn("key-1", seen)
        self.assertNotIn("key-2", seen)


if __name__ == "__main__":
    unittest.main()
