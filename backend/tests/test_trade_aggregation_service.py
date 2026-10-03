"""Unit tests for the trade aggregation buffer (``app.services.trade_aggregation_service``).

Followed traders rarely emit one clean order; they dribble small trades. This
service buffers those trades per ``(user_id, token_id, side)`` and hands back a
single volume-weighted order once the window has elapsed (or on a forced flush).

The module keeps its state in module-level dicts, so every test resets them
before and after itself to keep the suite order-independent. Settings are
injected through a stub because the two aggregation windows are read with
``getattr(settings, ..., default)`` -- i.e. they are optional settings that fall
back to module defaults, which these tests exercise explicitly.

Covered:

* ``add_trade_to_buffer`` -- disabled/negative windows, side normalisation,
  metadata defaults, buffer-key isolation and the first-trade timer.
* ``get_aggregated_trade`` -- empty buffers, window not elapsed, window elapsed,
  the max-window override, VWAP maths, ISO-serialised individual trades and the
  buffer/timer cleanup (including the zero-size and missing-timer boundaries).
* ``flush_buffer`` -- forced flush that ignores the window entirely.
* ``get_pending_buffers`` -- summary key format, token truncation, totals.
* ``_aggregation_loop`` -- dispatch to ``execute_aggregated_trade``, and each of
  its four error paths (execution failure, missing helper, outer failure,
  cancellation flush).
* ``start_aggregation_service`` / ``stop_aggregation_service`` -- idempotency,
  restart after completion and cancellation.
"""

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import trade_aggregation_service as tas
from app.services.trade_aggregation_service import (
    DEFAULT_MAX_WINDOW_SECONDS,
    DEFAULT_WINDOW_SECONDS,
    add_trade_to_buffer,
    flush_buffer,
    get_aggregated_trade,
    get_pending_buffers,
    start_aggregation_service,
    stop_aggregation_service,
)
from app.utils.time import utc_now

TOKEN = "0x" + "a" * 40
LONG_TOKEN = "token-id-" + "b" * 40


class _AggregationTestCase(unittest.TestCase):
    """Shared buffer reset plus a settings stub helper."""

    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)

    def _settings(self, window=DEFAULT_WINDOW_SECONDS, max_window=DEFAULT_MAX_WINDOW_SECONDS):
        """Patch ``get_settings`` with explicit aggregation windows."""
        stub = SimpleNamespace(
            trade_aggregation_window=window, trade_aggregation_max_window=max_window
        )
        patcher = patch.object(tas, "get_settings", return_value=stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        return stub

    def _settings_without_window_attributes(self):
        """A settings object that declares neither window (default path)."""
        stub = SimpleNamespace()
        patcher = patch.object(tas, "get_settings", return_value=stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        return stub

    def _buffer(self, user_id=7, token_id=TOKEN, side="BUY"):
        return tas._pending_buffer[(user_id, token_id, side)]

    def _age_buffer(self, seconds, user_id=7, token_id=TOKEN, side="BUY"):
        """Backdate the timer so the window has elapsed by ``seconds``."""
        tas._buffer_timers[(user_id, token_id, side)] = utc_now() - timedelta(seconds=seconds)


def _reset_state():
    tas._pending_buffer.clear()
    tas._buffer_timers.clear()


class AddTradeToBufferTests(_AggregationTestCase):
    """Buffering, rejection and bookkeeping on insert."""

    def test_zero_window_disables_aggregation_and_buffers_nothing(self):
        self._settings(window=0)
        self.assertFalse(add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 100.0))
        self.assertEqual(dict(tas._pending_buffer), {})

    def test_negative_window_disables_aggregation(self):
        self._settings(window=-5)
        self.assertFalse(add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 100.0))
        self.assertEqual(tas._buffer_timers, {})

    def test_enabled_window_reports_that_the_caller_should_wait(self):
        self._settings(window=30)
        self.assertTrue(add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 100.0))
        self.assertEqual(len(self._buffer()), 1)

    def test_entry_records_price_size_timestamp_and_empty_metadata(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.42, 12.5)
        (entry,) = self._buffer()
        self.assertEqual(set(entry), {"price", "size", "added_at", "metadata"})
        self.assertEqual(entry["price"], 0.42)
        self.assertEqual(entry["size"], 12.5)
        self.assertEqual(entry["metadata"], {})
        self.assertIsNotNone(entry["added_at"].tzinfo)

    def test_metadata_is_stored_when_supplied(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.42, 12.5, metadata={"source": "leaderboard"})
        self.assertEqual(self._buffer()[0]["metadata"], {"source": "leaderboard"})

    def test_side_is_normalised_to_upper_case(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "buy", 0.42, 12.5)
        self.assertIn((7, TOKEN, "BUY"), tas._pending_buffer)
        self.assertIn((7, TOKEN, "BUY"), tas._buffer_timers)

    def test_side_is_normalised_on_the_other_leg_too(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "sell", 0.58, 12.5)
        self.assertIn((7, TOKEN, "SELL"), tas._pending_buffer)

    def test_timer_is_stamped_only_by_the_first_trade(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        first_timer = tas._buffer_timers[(7, TOKEN, "BUY")]
        add_trade_to_buffer(7, TOKEN, "BUY", 0.5, 20.0)
        self.assertIs(tas._buffer_timers[(7, TOKEN, "BUY")], first_timer)
        self.assertEqual(len(self._buffer()), 2)

    def test_user_token_and_side_produce_independent_buffers(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        add_trade_to_buffer(8, TOKEN, "BUY", 0.4, 10.0)
        add_trade_to_buffer(7, LONG_TOKEN, "BUY", 0.4, 10.0)
        add_trade_to_buffer(7, TOKEN, "SELL", 0.6, 10.0)

        self.assertEqual(len(tas._pending_buffer), 4)
        self.assertEqual(len(tas._buffer_timers), 4)
        self.assertEqual(len(get_pending_buffers()), 4)

    def test_missing_window_setting_falls_back_to_the_module_default(self):
        self._settings_without_window_attributes()
        self.assertTrue(add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0))
        self.assertEqual(len(tas._pending_buffer), 1)

    def test_zero_size_trades_are_still_buffered(self):
        # No validation on size: a zero fill must not silently vanish from the
        # buffer, otherwise the trade count reported downstream is wrong.
        self._settings()
        self.assertTrue(add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 0.0))
        self.assertEqual(self._buffer()[0]["size"], 0.0)


class GetAggregatedTradeTests(_AggregationTestCase):
    """Window gating, VWAP aggregation and buffer consumption."""

    def test_unknown_key_returns_none(self):
        self._settings()
        self.assertIsNone(get_aggregated_trade(99, TOKEN, "BUY"))

    def test_empty_buffer_returns_none(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        tas._pending_buffer[(7, TOKEN, "BUY")].clear()
        self.assertIsNone(get_aggregated_trade(7, TOKEN, "BUY"))

    def test_buffer_without_a_timer_returns_none(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        del tas._buffer_timers[(7, TOKEN, "BUY")]
        self.assertIsNone(get_aggregated_trade(7, TOKEN, "BUY"))

    def test_trades_inside_the_window_are_kept(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        self.assertIsNone(get_aggregated_trade(7, TOKEN, "BUY"))
        self.assertEqual(len(self._buffer()), 1)
        self.assertIn((7, TOKEN, "BUY"), tas._buffer_timers)

    def test_elapsed_window_aggregates_and_clears_the_buffer(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "buy", 0.40, 100.0)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.50, 200.0, metadata={"k": "v"})
        self._age_buffer(35)

        result = get_aggregated_trade(7, TOKEN, "buy")

        self.assertEqual(result["aggregated_size"], 300.0)
        self.assertEqual(result["vwap_price"], round((100 * 0.40 + 200 * 0.50) / 300, 4))
        self.assertEqual(result["vwap_price"], 0.4667)
        self.assertEqual(result["trade_count"], 2)
        self.assertEqual(result["user_id"], 7)
        self.assertEqual(result["token_id"], TOKEN)
        self.assertEqual(result["side"], "BUY")
        self.assertNotIn((7, TOKEN, "BUY"), tas._pending_buffer)
        self.assertNotIn((7, TOKEN, "BUY"), tas._buffer_timers)

    def test_window_seconds_reports_the_elapsed_time(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        self._age_buffer(35)
        result = get_aggregated_trade(7, TOKEN, "BUY")
        self.assertAlmostEqual(result["window_seconds"], 35.0, delta=0.5)

    def test_individual_trades_are_isoformatted_without_metadata(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0, metadata={"ignored": True})
        added_at = self._buffer()[0]["added_at"]
        self._age_buffer(31)

        result = get_aggregated_trade(7, TOKEN, "BUY")

        self.assertEqual(
            result["individual_trades"],
            [{"price": 0.4, "size": 10.0, "added_at": added_at.isoformat()}],
        )

    def test_second_call_after_aggregation_returns_none(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        self._age_buffer(31)
        get_aggregated_trade(7, TOKEN, "BUY")
        self.assertIsNone(get_aggregated_trade(7, TOKEN, "BUY"))

    def test_max_window_releases_the_buffer_before_the_regular_window(self):
        # window=600 has not elapsed at 40s, but max_window=30 forces execution
        # so a continuously busy key cannot starve forever.
        self._settings(window=600, max_window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        self._age_buffer(40)
        result = get_aggregated_trade(7, TOKEN, "BUY")
        self.assertIsNotNone(result)
        self.assertEqual(result["aggregated_size"], 10.0)

    def test_max_window_boundary_does_not_release_early(self):
        self._settings(window=600, max_window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        self._age_buffer(29)
        self.assertIsNone(get_aggregated_trade(7, TOKEN, "BUY"))

    def test_zero_total_size_yields_zero_vwap(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 0.0)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.6, 0.0)
        self._age_buffer(31)

        result = get_aggregated_trade(7, TOKEN, "BUY")

        self.assertEqual(result["aggregated_size"], 0.0)
        self.assertEqual(result["vwap_price"], 0)
        self.assertEqual(result["trade_count"], 2)

    def test_single_trade_aggregates_to_its_own_price(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.375, 40.0)
        self._age_buffer(30)
        result = get_aggregated_trade(7, TOKEN, "BUY")
        self.assertEqual(result["vwap_price"], 0.375)
        self.assertEqual(result["aggregated_size"], 40.0)

    def test_aggregation_is_keyed_per_user_and_side(self):
        self._settings(window=30)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.40, 100.0)
        add_trade_to_buffer(7, TOKEN, "SELL", 0.60, 50.0)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.60, 50.0)
        self._age_buffer(31, side="BUY")
        self._age_buffer(31, side="SELL")

        sell = get_aggregated_trade(7, TOKEN, "SELL")
        buy = get_aggregated_trade(7, TOKEN, "BUY")

        self.assertEqual(sell["side"], "SELL")
        self.assertEqual(sell["aggregated_size"], 50.0)
        self.assertEqual(sell["trade_count"], 1)
        self.assertEqual(buy["aggregated_size"], 150.0)
        self.assertEqual(buy["trade_count"], 2)


class FlushBufferTests(_AggregationTestCase):
    """Forced flushes, which deliberately ignore both windows."""

    def test_empty_buffer_returns_none(self):
        self._settings()
        self.assertIsNone(flush_buffer(7, TOKEN, "BUY"))

    def test_flush_returns_the_aggregate_immediately(self):
        self._settings(window=3600)
        add_trade_to_buffer(7, TOKEN, "buy", 0.40, 100.0)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.60, 100.0)

        result = flush_buffer(7, TOKEN, "buy")

        self.assertEqual(
            result,
            {
                "user_id": 7,
                "token_id": TOKEN,
                "side": "BUY",
                "aggregated_size": 200.0,
                "vwap_price": 0.5,
                "trade_count": 2,
                "flushed": True,
            },
        )

    def test_flush_clears_the_buffer_and_the_timer(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        flush_buffer(7, TOKEN, "BUY")
        self.assertNotIn((7, TOKEN, "BUY"), tas._pending_buffer)
        self.assertNotIn((7, TOKEN, "BUY"), tas._buffer_timers)
        self.assertEqual(get_pending_buffers(), {})

    def test_flushed_result_has_no_window_timing(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        result = flush_buffer(7, TOKEN, "BUY")
        self.assertNotIn("window_seconds", result)

    def test_second_flush_returns_none(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        flush_buffer(7, TOKEN, "BUY")
        self.assertIsNone(flush_buffer(7, TOKEN, "BUY"))

    def test_zero_size_flush_reports_zero_vwap(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 0.0)
        result = flush_buffer(7, TOKEN, "BUY")
        self.assertEqual(result["vwap_price"], 0)
        self.assertEqual(result["aggregated_size"], 0.0)

    def test_flush_leaves_other_keys_untouched(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        add_trade_to_buffer(7, TOKEN, "SELL", 0.6, 20.0)
        flush_buffer(7, TOKEN, "BUY")
        self.assertIn((7, TOKEN, "SELL"), tas._pending_buffer)


class PendingBuffersTests(_AggregationTestCase):
    """Read-only snapshot of what the background loop still has to process."""

    def test_no_buffers_returns_an_empty_summary(self):
        self._settings()
        self.assertEqual(get_pending_buffers(), {})

    def test_summary_key_is_user_token_prefix_side(self):
        self._settings()
        add_trade_to_buffer(42, TOKEN, "buy", 0.4, 10.0)
        summary = get_pending_buffers()
        self.assertEqual(list(summary), [f"42:{TOKEN[:16]}:BUY"])

    def test_long_token_ids_are_truncated_in_the_summary_key(self):
        self._settings()
        add_trade_to_buffer(1, LONG_TOKEN, "SELL", 0.6, 10.0)
        self.assertEqual(list(get_pending_buffers()), [f"1:{LONG_TOKEN[:16]}:SELL"])

    def test_summary_reports_count_and_total_size(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.40, 10.0)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.45, 15.5)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.50, 20.0)

        entry = get_pending_buffers()[f"7:{TOKEN[:16]}:BUY"]

        self.assertEqual(entry["count"], 3)
        self.assertEqual(entry["total_size"], 45.5)

    def test_summary_reports_when_the_key_started_waiting(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        started = tas._buffer_timers[(7, TOKEN, "BUY")]
        entry = get_pending_buffers()[f"7:{TOKEN[:16]}:BUY"]
        self.assertEqual(entry["waiting_since"], started.isoformat())

    def test_summary_tolerates_a_missing_timer(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        del tas._buffer_timers[(7, TOKEN, "BUY")]
        self.assertIsNone(get_pending_buffers()[f"7:{TOKEN[:16]}:BUY"]["waiting_since"])

    def test_summary_covers_every_pending_key(self):
        self._settings()
        add_trade_to_buffer(7, TOKEN, "BUY", 0.4, 10.0)
        add_trade_to_buffer(9, TOKEN, "SELL", 0.6, 30.0)
        summary = get_pending_buffers()
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[f"9:{TOKEN[:16]}:SELL"]["total_size"], 30.0)


class AggregationLoopTests(unittest.IsolatedAsyncioTestCase):
    """The background loop that executes ready aggregates."""

    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)
        settings = SimpleNamespace(trade_aggregation_window=30, trade_aggregation_max_window=120)
        patcher = patch.object(tas, "get_settings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_ready_buffer(self):
        add_trade_to_buffer(7, TOKEN, "BUY", 0.40, 100.0)
        add_trade_to_buffer(7, TOKEN, "BUY", 0.60, 100.0)
        tas._buffer_timers[(7, TOKEN, "BUY")] = utc_now() - timedelta(seconds=31)

    def _stop_after_first_pass(self):
        """Cancel the loop at the sleep that follows the first scan."""
        return patch.object(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError("stop")))

    async def test_ready_aggregate_is_handed_to_the_execution_helper(self):
        self._seed_ready_buffer()
        execute = AsyncMock(return_value={"success": True})

        with (
            patch("app.services.copy_trade_service.execute_aggregated_trade", execute),
            self._stop_after_first_pass(),
            self.assertRaises(asyncio.CancelledError),
        ):
            await tas._aggregation_loop()

        execute.assert_awaited_once()
        aggregated = execute.await_args.args[0]
        self.assertEqual(aggregated["aggregated_size"], 200.0)
        self.assertEqual(aggregated["vwap_price"], 0.5)
        self.assertEqual(aggregated["trade_count"], 2)
        self.assertEqual(aggregated["user_id"], 7)
        self.assertEqual(dict(tas._pending_buffer), {})

    async def test_not_yet_ready_buffer_is_not_executed(self):
        add_trade_to_buffer(7, TOKEN, "BUY", 0.40, 100.0)
        execute = AsyncMock()

        with (
            patch("app.services.copy_trade_service.execute_aggregated_trade", execute),
            self._stop_after_first_pass(),
            self.assertRaises(asyncio.CancelledError),
        ):
            await tas._aggregation_loop()

        execute.assert_not_awaited()
        self.assertIn((7, TOKEN, "BUY"), tas._pending_buffer)

    async def test_execution_failure_is_logged_and_the_loop_continues(self):
        self._seed_ready_buffer()
        execute = AsyncMock(side_effect=RuntimeError("order rejected"))

        with (
            patch("app.services.copy_trade_service.execute_aggregated_trade", execute),
            self.assertLogs(tas.logger, level="ERROR") as captured,
            self._stop_after_first_pass() as sleep,
            self.assertRaises(asyncio.CancelledError),
        ):
            await tas._aggregation_loop()

        self.assertTrue(
            any("Failed to execute aggregated trade" in line for line in captured.output)
        )
        self.assertTrue(any("order rejected" in line for line in captured.output))
        sleep.assert_awaited_once_with(5)

    async def test_missing_execution_helper_degrades_to_logging_only(self):
        self._seed_ready_buffer()
        import app.services.copy_trade_service as copy_trade_service

        helper = copy_trade_service.execute_aggregated_trade
        del copy_trade_service.execute_aggregated_trade
        try:
            with (
                self.assertLogs(tas.logger, level="DEBUG") as captured,
                self._stop_after_first_pass(),
                self.assertRaises(asyncio.CancelledError),
            ):
                await tas._aggregation_loop()
        finally:
            copy_trade_service.execute_aggregated_trade = helper

        self.assertTrue(
            any("execute_aggregated_trade not available yet" in line for line in captured.output)
        )

    async def test_scan_failure_is_logged_and_the_loop_continues(self):
        add_trade_to_buffer(7, TOKEN, "BUY", 0.40, 100.0)
        scan = MagicMock(side_effect=[ValueError("bad state"), None])
        # The first pass fails, so the loop sleeps and scans again; the second
        # sleep cancels to end the test.
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError("stop")])

        with (
            patch.object(tas, "get_aggregated_trade", scan),
            self.assertLogs(tas.logger, level="ERROR") as captured,
            patch.object(asyncio, "sleep", sleep),
            self.assertRaises(asyncio.CancelledError),
        ):
            await tas._aggregation_loop()

        self.assertTrue(any("Aggregation loop error" in line for line in captured.output))
        self.assertTrue(any("bad state" in line for line in captured.output))
        self.assertEqual(scan.call_count, 2)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [5, 5])

    async def test_cancellation_flushes_the_remaining_buffers(self):
        add_trade_to_buffer(7, TOKEN, "BUY", 0.40, 100.0)
        add_trade_to_buffer(8, TOKEN, "SELL", 0.60, 50.0)

        with (
            patch.object(
                tas, "get_aggregated_trade", MagicMock(side_effect=asyncio.CancelledError)
            ),
            self.assertLogs(tas.logger, level="INFO") as captured,
        ):
            await tas._aggregation_loop()

        self.assertEqual(dict(tas._pending_buffer), {})
        self.assertEqual(tas._buffer_timers, {})
        flushed = [
            line for line in captured.output if "Flushed pending aggregation on shutdown" in line
        ]
        self.assertEqual(len(flushed), 2)

    async def test_loop_announces_startup_and_shutdown(self):
        add_trade_to_buffer(7, TOKEN, "BUY", 0.40, 100.0)
        with (
            patch.object(
                tas, "get_aggregated_trade", MagicMock(side_effect=asyncio.CancelledError)
            ),
            self.assertLogs(tas.logger, level="INFO") as captured,
        ):
            await tas._aggregation_loop()

        self.assertTrue(
            any("Trade aggregation service started" in line for line in captured.output)
        )
        self.assertTrue(
            any("Trade aggregation service stopped" in line for line in captured.output)
        )

    async def test_shutdown_skips_a_buffer_entry_with_no_trades(self):
        # An entry can be left behind empty if another code path clears the
        # list in place; flush_buffer reports None and the loop must not treat
        # that as an error.
        tas._pending_buffer[(7, TOKEN, "BUY")] = []

        with (
            patch.object(
                tas, "get_aggregated_trade", MagicMock(side_effect=asyncio.CancelledError)
            ),
            self.assertLogs(tas.logger, level="INFO") as captured,
        ):
            await tas._aggregation_loop()

        self.assertFalse(any("Flushed pending aggregation" in line for line in captured.output))
        self.assertTrue(
            any("Trade aggregation service stopped" in line for line in captured.output)
        )


class AggregationServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Start/stop of the background task handle."""

    def setUp(self):
        _reset_state()
        self.addCleanup(_reset_state)

    async def asyncTearDown(self):
        await stop_aggregation_service()

    async def test_start_creates_the_aggregation_loop_task(self):
        await start_aggregation_service()
        task = tas._aggregation_task
        self.assertIsInstance(task, asyncio.Task)
        self.assertEqual(task.get_coro().__name__, "_aggregation_loop")

    async def test_second_start_while_running_keeps_the_same_task(self):
        await start_aggregation_service()
        first = tas._aggregation_task
        await start_aggregation_service()
        self.assertIs(tas._aggregation_task, first)

    async def test_stop_cancels_the_task_and_clears_the_handle(self):
        await start_aggregation_service()
        task = tas._aggregation_task
        await stop_aggregation_service()
        self.assertIsNone(tas._aggregation_task)
        self.assertTrue(task.cancelled() or task.done())

    async def test_stop_without_a_task_is_a_no_op(self):
        tas._aggregation_task = None
        await stop_aggregation_service()
        self.assertIsNone(tas._aggregation_task)

    async def test_stop_after_the_task_finished_still_clears_the_handle(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        tas._aggregation_task = finished

        await stop_aggregation_service()

        self.assertIsNone(tas._aggregation_task)
        self.assertFalse(finished.cancelled())
        self.assertTrue(finished.done())

    async def test_start_replaces_a_finished_task(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        tas._aggregation_task = finished

        await start_aggregation_service()

        self.assertIsNot(tas._aggregation_task, finished)

    async def test_start_then_stop_leaves_no_pending_tasks(self):
        await start_aggregation_service()
        await stop_aggregation_service()
        self.assertIsNone(tas._aggregation_task)


if __name__ == "__main__":
    unittest.main()
