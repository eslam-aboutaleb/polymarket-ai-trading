"""Coverage tests for ``app.services.arbitrage_service``.

Exercises both arbitrage detectors (complement and spread)
with every guard branch, the market scan (including upstream
failure isolation), the background monitor loop (lock
acquisition, opportunity capping, error containment,
heartbeat, lock release), and the start/stop lifecycle.
All external I/O (Polymarket API, scheduler locks, sleeps)
is mocked.
"""

import asyncio
import contextlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import arbitrage_service as arb_mod
from app.services.arbitrage_service import (
    _check_complement_arbitrage,
    _check_spread_arbitrage,
    _fetch_markets_with_rate_limit,
    get_recent_opportunities,
    scan_for_arbitrage,
    start_arbitrage_monitor,
    stop_arbitrage_monitor,
)


class _ArbitrageTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_opps = arb_mod._arb_opportunities
        self._orig_task = arb_mod._arb_task
        arb_mod._arb_opportunities = []
        arb_mod._arb_task = None
        self.addCleanup(self._restore_state)

    def _restore_state(self):
        arb_mod._arb_opportunities = self._orig_opps
        arb_mod._arb_task = self._orig_task


class ComplementArbitrageTests(unittest.TestCase):
    def test_detects_guaranteed_profit(self):
        opportunity = _check_complement_arbitrage(
            {
                "condition_id": "0xcond-1",
                "question": "Will it rain?",
                "outcomePrices": [0.45, 0.50],
            }
        )
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity["type"], "complement")
        self.assertEqual(opportunity["market_id"], "0xcond-1")
        self.assertEqual(opportunity["market_title"], "Will it rain?")
        self.assertEqual(opportunity["prices"], [0.45, 0.50])
        self.assertEqual(opportunity["total_cost"], 0.95)
        self.assertEqual(opportunity["profit_pct"], 5.0)
        self.assertIn("detected_at", opportunity)

    def test_accepts_string_prices(self):
        opportunity = _check_complement_arbitrage({"outcomePrices": ["0.45", "0.50"]})
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity["total_cost"], 0.95)

    def test_condition_id_falls_back_to_conditionId(self):
        opportunity = _check_complement_arbitrage(
            {"conditionId": "0xcamel", "outcomePrices": [0.4, 0.5]}
        )
        self.assertEqual(opportunity["market_id"], "0xcamel")

    def test_title_falls_back_to_event_title_then_unknown(self):
        opportunity = _check_complement_arbitrage(
            {"_event_title": "Event Title", "outcomePrices": [0.4, 0.5]}
        )
        self.assertEqual(opportunity["market_title"], "Event Title")
        opportunity = _check_complement_arbitrage({"outcomePrices": [0.4, 0.5]})
        self.assertEqual(opportunity["market_title"], "Unknown")

    def test_non_list_outcome_prices_ignored(self):
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": "0.45,0.50"}))
        self.assertIsNone(_check_complement_arbitrage({}))

    def test_single_outcome_price_ignored(self):
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": [0.45]}))

    def test_non_numeric_prices_ignored(self):
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": ["a", "b"]}))
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": [None, 0.5]}))

    def test_non_positive_prices_ignored(self):
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": [0.5, 0]}))
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": [0.5, -0.1]}))

    def test_no_edge_at_or_above_threshold(self):
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": [0.5, 0.5]}))
        self.assertIsNone(_check_complement_arbitrage({"outcomePrices": [0.49, 0.49]}))


class SpreadArbitrageTests(unittest.TestCase):
    def test_detects_large_spread(self):
        opportunity = _check_spread_arbitrage(
            {
                "condition_id": "0xcond-2",
                "question": "Spread market",
                "bestBid": 0.9,
                "bestAsk": 1.0,
            }
        )
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity["type"], "spread")
        self.assertEqual(opportunity["market_id"], "0xcond-2")
        self.assertEqual(opportunity["market_title"], "Spread market")
        self.assertEqual(opportunity["token_id"], "")
        self.assertEqual(opportunity["outcome"], "Yes")
        self.assertEqual(opportunity["bid"], 0.9)
        self.assertEqual(opportunity["ask"], 1.0)
        self.assertEqual(opportunity["spread_pct"], 10.0)
        self.assertIn("detected_at", opportunity)

    def test_condition_id_falls_back_to_conditionId(self):
        opportunity = _check_spread_arbitrage(
            {"conditionId": "0xcamel", "bestBid": 0.9, "bestAsk": 1.0}
        )
        self.assertEqual(opportunity["market_id"], "0xcamel")

    def test_title_falls_back_to_event_title_then_unknown(self):
        opportunity = _check_spread_arbitrage(
            {"_event_title": "Event", "bestBid": 0.9, "bestAsk": 1.0}
        )
        self.assertEqual(opportunity["market_title"], "Event")
        opportunity = _check_spread_arbitrage({"bestBid": 0.9, "bestAsk": 1.0})
        self.assertEqual(opportunity["market_title"], "Unknown")

    def test_missing_bid_or_ask_ignored(self):
        self.assertIsNone(_check_spread_arbitrage({"bestAsk": 1.0}))
        self.assertIsNone(_check_spread_arbitrage({"bestBid": 0.9}))
        self.assertIsNone(_check_spread_arbitrage({}))

    def test_non_numeric_bid_ask_ignored(self):
        self.assertIsNone(_check_spread_arbitrage({"bestBid": "n/a", "bestAsk": 1.0}))
        self.assertIsNone(_check_spread_arbitrage({"bestBid": 0.9, "bestAsk": None}))

    def test_non_positive_bid_ask_ignored(self):
        self.assertIsNone(_check_spread_arbitrage({"bestBid": 0, "bestAsk": 1.0}))
        self.assertIsNone(_check_spread_arbitrage({"bestBid": 0.9, "bestAsk": 0}))

    def test_inverted_book_ignored(self):
        self.assertIsNone(_check_spread_arbitrage({"bestBid": 1.0, "bestAsk": 0.9}))
        self.assertIsNone(_check_spread_arbitrage({"bestBid": 0.9, "bestAsk": 0.9}))

    def test_spread_below_threshold_ignored(self):
        self.assertIsNone(_check_spread_arbitrage({"bestBid": 0.96, "bestAsk": 1.0}))

    def test_spread_at_threshold_detected(self):
        opportunity = _check_spread_arbitrage({"bestBid": 0.95, "bestAsk": 1.0})
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity["spread_pct"], 5.0)


class FetchMarketsWithRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_service_with_limit(self):
        service = MagicMock()
        service.get_active_markets = AsyncMock(return_value=[{"condition_id": "c1"}])
        markets = await _fetch_markets_with_rate_limit(service)
        self.assertEqual(markets, [{"condition_id": "c1"}])
        service.get_active_markets.assert_awaited_once_with(limit=100)


class ScanForArbitrageTests(unittest.IsolatedAsyncioTestCase):
    def _patch_service(self, markets=None, side_effect=None):
        instance = MagicMock()
        instance.get_active_markets = AsyncMock(return_value=markets, side_effect=side_effect)
        patcher = patch(
            "app.services.polymarket_service.get_polymarket_service",
            return_value=instance,
        )
        factory = patcher.start()
        self.addCleanup(patcher.stop)
        return factory, instance

    async def test_collects_both_opportunity_types(self):
        self._patch_service(
            markets=[
                {
                    "condition_id": "c1",
                    "question": "Complement",
                    "outcomePrices": [0.45, 0.50],
                },
                {
                    "condition_id": "c2",
                    "question": "Spread",
                    "bestBid": 0.9,
                    "bestAsk": 1.0,
                },
                {
                    "condition_id": "c3",
                    "question": "Nothing",
                    "outcomePrices": [0.5, 0.5],
                    "bestBid": 0.96,
                    "bestAsk": 1.0,
                },
            ]
        )
        opportunities = await scan_for_arbitrage()
        self.assertEqual(len(opportunities), 2)
        self.assertEqual(opportunities[0]["type"], "complement")
        self.assertEqual(opportunities[1]["type"], "spread")

    async def test_empty_market_list(self):
        factory, instance = self._patch_service(markets=[])
        self.assertEqual(await scan_for_arbitrage(), [])
        instance.get_active_markets.assert_awaited_once_with(limit=100)
        factory.assert_called_once_with()

    async def test_fetch_failure_returns_empty(self):
        self._patch_service(side_effect=RuntimeError("gateway timeout"))
        with self.assertLogs(arb_mod.logger, level="ERROR") as captured:
            self.assertEqual(await scan_for_arbitrage(), [])
        self.assertTrue(any("Failed to fetch markets" in line for line in captured.output))


class ArbMonitorLoopTests(_ArbitrageTestCase):
    async def test_skips_when_lock_not_acquired(self):
        scan = AsyncMock()
        release = MagicMock()
        with (
            patch.object(arb_mod, "acquire_scheduler_lock", return_value=False),
            patch.object(arb_mod, "scan_for_arbitrage", scan),
            patch.object(arb_mod, "release_scheduler_lock", release),
        ):
            await arb_mod._arb_monitor_loop()
        scan.assert_not_awaited()
        release.assert_not_called()

    async def test_stores_opportunities_and_releases_lock(self):
        opportunity = {"type": "complement", "market_id": "c1"}
        scan = AsyncMock(side_effect=[[opportunity], []])
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError("stop")])
        with (
            patch.object(arb_mod, "acquire_scheduler_lock", return_value=True),
            patch.object(arb_mod, "release_scheduler_lock") as release,
            patch.object(arb_mod, "scheduler_heartbeat") as heartbeat,
            patch.object(arb_mod, "scan_for_arbitrage", scan),
            patch.object(asyncio, "sleep", sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await arb_mod._arb_monitor_loop()
        self.assertEqual(arb_mod._arb_opportunities, [opportunity])
        heartbeat.assert_called_with("arbitrage_monitor")
        release.assert_called_once_with("arbitrage_monitor")

    async def test_caps_stored_opportunities_at_one_hundred(self):
        first_batch = [{"i": i} for i in range(60)]
        second_batch = [{"i": i} for i in range(60, 120)]
        scan = AsyncMock(side_effect=[first_batch, second_batch])
        sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError("stop")])
        with (
            patch.object(arb_mod, "acquire_scheduler_lock", return_value=True),
            patch.object(arb_mod, "release_scheduler_lock"),
            patch.object(arb_mod, "scheduler_heartbeat"),
            patch.object(arb_mod, "scan_for_arbitrage", scan),
            patch.object(asyncio, "sleep", sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await arb_mod._arb_monitor_loop()
        self.assertEqual(len(arb_mod._arb_opportunities), 100)
        self.assertEqual(arb_mod._arb_opportunities[0]["i"], 60)

    async def test_scan_error_is_logged_and_loop_continues(self):
        scan = AsyncMock(side_effect=RuntimeError("scan boom"))
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError("stop")])
        with (
            patch.object(arb_mod, "acquire_scheduler_lock", return_value=True),
            patch.object(arb_mod, "release_scheduler_lock"),
            patch.object(arb_mod, "scheduler_heartbeat"),
            patch.object(arb_mod, "scan_for_arbitrage", scan),
            patch.object(asyncio, "sleep", sleep),
            self.assertLogs(arb_mod.logger, level="ERROR") as captured,
            self.assertRaises(asyncio.CancelledError),
        ):
            await arb_mod._arb_monitor_loop()
        self.assertEqual(scan.await_count, 2)
        self.assertTrue(any("Arbitrage scan error" in line for line in captured.output))

    async def test_logs_found_opportunity_count(self):
        scan = AsyncMock(return_value=[{"type": "spread"}])
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError("stop")])
        with (
            patch.object(arb_mod, "acquire_scheduler_lock", return_value=True),
            patch.object(arb_mod, "release_scheduler_lock"),
            patch.object(arb_mod, "scheduler_heartbeat"),
            patch.object(arb_mod, "scan_for_arbitrage", scan),
            patch.object(asyncio, "sleep", sleep),
            self.assertLogs(arb_mod.logger, level="INFO") as captured,
            self.assertRaises(asyncio.CancelledError),
        ):
            await arb_mod._arb_monitor_loop()
        self.assertTrue(any("Found 1 arbitrage opportunities" in line for line in captured.output))


class MonitorLifecycleTests(_ArbitrageTestCase):
    def _patches(self):
        return [
            patch.object(arb_mod, "acquire_scheduler_lock", return_value=True),
            patch.object(arb_mod, "scan_for_arbitrage", AsyncMock(return_value=[])),
            patch.object(arb_mod, "scheduler_heartbeat"),
        ]

    def _enter_patches(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for patcher in self._patches():
            stack.enter_context(patcher)

    async def test_start_creates_task_and_stop_cancels_it(self):
        self._enter_patches()
        await start_arbitrage_monitor()
        task = arb_mod._arb_task
        self.assertIsInstance(task, asyncio.Task)
        await asyncio.sleep(0)
        await stop_arbitrage_monitor()
        self.assertIsNone(arb_mod._arb_task)
        self.assertTrue(task.cancelled())

    async def test_second_start_keeps_running_task(self):
        self._enter_patches()
        await start_arbitrage_monitor()
        first = arb_mod._arb_task
        await start_arbitrage_monitor()
        self.assertIs(arb_mod._arb_task, first)
        await stop_arbitrage_monitor()

    async def test_start_replaces_finished_task(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        arb_mod._arb_task = finished
        self._enter_patches()
        await start_arbitrage_monitor()
        self.assertIsNot(arb_mod._arb_task, finished)
        self.assertFalse(arb_mod._arb_task.done())
        await stop_arbitrage_monitor()

    async def test_stop_without_task_is_a_no_op(self):
        arb_mod._arb_task = None
        await stop_arbitrage_monitor()
        self.assertIsNone(arb_mod._arb_task)

    async def test_stop_with_finished_task_clears_handle(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        arb_mod._arb_task = finished
        await stop_arbitrage_monitor()
        self.assertIsNone(arb_mod._arb_task)
        self.assertFalse(finished.cancelled())
        self.assertTrue(finished.done())


class RecentOpportunitiesTests(unittest.TestCase):
    def test_empty_by_default(self):
        arb_mod._arb_opportunities = []
        self.assertEqual(get_recent_opportunities(), [])

    def test_returns_most_recent_fifty(self):
        arb_mod._arb_opportunities = [{"i": i} for i in range(60)]
        recent = get_recent_opportunities()
        self.assertEqual(len(recent), 50)
        self.assertEqual(recent[0]["i"], 0)
        self.assertEqual(recent[-1]["i"], 49)


if __name__ == "__main__":
    unittest.main()
