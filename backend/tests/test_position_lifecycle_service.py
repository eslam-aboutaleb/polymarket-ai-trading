"""Behavioural tests for :mod:`app.services.position_lifecycle_service`.

Covers the whole position lifecycle engine:

* ``_check_resolved_markets`` – which Polymarket positions count as resolved
  (``resolved`` / ``closed``), the ``conditionId``/``condition_id`` and
  ``question``/``title`` key fallbacks, and the non-zero-size requirement.
* ``_check_stale_positions`` – the buy-trade query built from the age cutoff,
  the reported payload shape, and the rounded ``age_hours``.
* ``_merge_duplicate_positions`` – the ``GROUP BY market_id HAVING count > 1``
  aggregation and its weighted-average entry price.
* ``run_lifecycle_check`` – the combined summary payload and session cleanup.
* ``_lifecycle_loop`` – per-user fan-out, per-user and outer error isolation,
  and clean shutdown on cancellation.
* ``start_position_lifecycle_manager`` / ``stop_position_lifecycle_manager``
  and the ``get_lifecycle_metrics`` snapshot.

Every external dependency (Polymarket service, SQLAlchemy session) is mocked;
no network or database access occurs.
"""

import asyncio
import contextlib
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import app.services.position_lifecycle_service as lifecycle
from app.models.user_trade import UserTrade
from app.utils.time import utc_now


class _LoopBreak(Exception):
    """Sentinel used to break out of the otherwise infinite lifecycle loop."""


def _canned(*results):
    """Side-effect callable yielding ``results`` in order.

    Raises ``AssertionError`` rather than ``StopIteration`` when exhausted, so
    an over-called mock fails loudly instead of hanging.
    """
    remaining = list(results)

    def _next(*_args, **_kwargs):
        if not remaining:
            raise AssertionError(f"side_effect exhausted after {len(results)} call(s)")
        result = remaining.pop(0)
        if isinstance(result, type) and issubclass(result, BaseException):
            result = result()
        if isinstance(result, BaseException):
            raise result
        return result

    return _next


async def _block_forever():
    """Stand-in for ``_lifecycle_loop`` that parks until it is cancelled."""
    await asyncio.Event().wait()


def _session(users=None):
    """Magic session whose ``query(...).all()`` yields ``users``."""
    db = MagicMock()
    db.query.return_value.all.return_value = [] if users is None else users
    return db


def _query_chain(result):
    """Chainable query mock whose ``.all()`` yields ``result``."""
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.group_by.return_value = query
    query.having.return_value = query
    query.all.return_value = result
    return query


def _lifecycle_result(resolved=0, stale=0, duplicates=0):
    """Minimal ``run_lifecycle_check`` payload for loop-level assertions."""
    return {
        "resolved_count": resolved,
        "stale_count": stale,
        "duplicate_count": duplicates,
    }


def _position(**market):
    """Build a Polymarket position payload with the given market fields."""
    return {"market": market}


class CheckResolvedMarketsTests(unittest.IsolatedAsyncioTestCase):
    """``_check_resolved_markets`` position classification."""

    def setUp(self):
        self.db = MagicMock()

    def _service(self, positions):
        service = MagicMock()
        service.get_positions = AsyncMock(return_value=positions)
        return service

    async def test_resolved_position_is_reported_with_full_detail(self):
        service = self._service(
            [
                {
                    "conditionId": "0xcond-1",
                    "size": "12.5",
                    "market": {
                        "resolved": True,
                        "question": "Will it rain?",
                        "outcomePrices": ["1", "0"],
                    },
                }
            ]
        )

        with patch("app.services.polymarket_service.get_polymarket_service", return_value=service):
            redeemed = await lifecycle._check_resolved_markets(self.db, 7, "0xaaa")

        service.get_positions.assert_awaited_once_with("0xaaa")
        self.assertEqual(
            redeemed,
            [
                {
                    "condition_id": "0xcond-1",
                    "title": "Will it rain?",
                    "size": 12.5,
                    "resolved": True,
                    "outcome_prices": ["1", "0"],
                }
            ],
        )

    async def test_closed_market_counts_as_resolved(self):
        service = self._service(
            [
                {
                    "condition_id": "0xcond-2",
                    "size": 3,
                    "market": {"closed": True, "title": "Settled market"},
                }
            ]
        )

        with patch("app.services.polymarket_service.get_polymarket_service", return_value=service):
            redeemed = await lifecycle._check_resolved_markets(self.db, 1, "0xbbb")

        self.assertEqual(len(redeemed), 1)
        self.assertEqual(redeemed[0]["condition_id"], "0xcond-2")
        self.assertEqual(redeemed[0]["title"], "Settled market")
        self.assertEqual(redeemed[0]["size"], 3.0)
        self.assertEqual(redeemed[0]["outcome_prices"], [])

    async def test_missing_title_falls_back_to_unknown(self):
        service = self._service(
            [{"conditionId": "0xcond-3", "size": 1, "market": {"resolved": True}}]
        )

        with patch("app.services.polymarket_service.get_polymarket_service", return_value=service):
            redeemed = await lifecycle._check_resolved_markets(self.db, 1, "0xbbb")

        self.assertEqual(redeemed[0]["title"], "Unknown")

    async def test_non_resolved_and_zero_size_positions_are_skipped(self):
        service = self._service(
            [
                {"size": 5, "market": {}},  # no market metadata
                {"size": 5, "market": {"resolved": False, "closed": False}},  # still open
                {"size": 0, "market": {"resolved": True}},  # fully exited
                {"size": -2, "market": {"closed": True}},  # negative size
                {"size": 2, "market": {"closed": True}},  # the only qualifying one
            ]
        )

        with patch("app.services.polymarket_service.get_polymarket_service", return_value=service):
            redeemed = await lifecycle._check_resolved_markets(self.db, 1, "0xbbb")

        self.assertEqual(len(redeemed), 1)
        self.assertEqual(redeemed[0]["size"], 2.0)

    async def test_no_positions_returns_empty_list(self):
        service = self._service([])

        with patch("app.services.polymarket_service.get_polymarket_service", return_value=service):
            redeemed = await lifecycle._check_resolved_markets(self.db, 1, "0xbbb")

        self.assertEqual(redeemed, [])

    async def test_service_failure_is_logged_and_returns_empty_list(self):
        service = MagicMock()
        service.get_positions = AsyncMock(side_effect=TimeoutError("data api down"))
        logger_mock = MagicMock()

        with (
            patch("app.services.polymarket_service.get_polymarket_service", return_value=service),
            patch.object(lifecycle, "logger", new=logger_mock),
        ):
            redeemed = await lifecycle._check_resolved_markets(self.db, 5, "0xccc")

        self.assertEqual(redeemed, [])
        self.assertEqual(
            logger_mock.error.call_args.args[0], "Error fetching positions for user %d: %s"
        )
        self.assertEqual(logger_mock.error.call_args.args[1], 5)

    async def test_malformed_size_is_skipped_and_later_positions_kept(self):
        """Regression: one bad entry must not discard the whole batch.

        The malformed entry used to raise inside a loop-wide ``try``, which
        discarded every position already collected for the user — including
        redeemable winnings.
        """
        service = self._service(
            [
                {"conditionId": "c1", "size": 5, "market": {"closed": True}},
                {"conditionId": "c2", "size": "not-a-number", "market": {"closed": True}},
                {"conditionId": "c3", "size": 7, "market": {"closed": True}},
            ]
        )
        logger_mock = MagicMock()

        with (
            patch("app.services.polymarket_service.get_polymarket_service", return_value=service),
            patch.object(lifecycle, "logger", new=logger_mock),
        ):
            redeemed = await lifecycle._check_resolved_markets(self.db, 5, "0xccc")

        # Both valid positions survive; only the malformed one is dropped.
        self.assertEqual([r["condition_id"] for r in redeemed], ["c1", "c3"])
        self.assertEqual([r["size"] for r in redeemed], [5.0, 7.0])
        # The skip is surfaced as a warning rather than silently ignored.
        logger_mock.warning.assert_called_once()
        self.assertIn("unparseable size", logger_mock.warning.call_args.args[0])

    async def test_non_finite_size_is_skipped(self):
        service = self._service(
            [
                {"conditionId": "nan", "size": float("nan"), "market": {"closed": True}},
                {"conditionId": "inf", "size": float("inf"), "market": {"closed": True}},
            ]
        )

        with patch("app.services.polymarket_service.get_polymarket_service", return_value=service):
            redeemed = await lifecycle._check_resolved_markets(self.db, 5, "0xccc")

        self.assertEqual(redeemed, [])

    async def test_non_dict_entry_is_skipped(self):
        service = self._service(["oops", None, 42])

        with patch("app.services.polymarket_service.get_polymarket_service", return_value=service):
            redeemed = await lifecycle._check_resolved_markets(self.db, 5, "0xccc")

        self.assertEqual(redeemed, [])


class CheckStalePositionsTests(unittest.IsolatedAsyncioTestCase):
    """``_check_stale_positions`` nets buys against sells before reporting age."""

    def setUp(self):
        self.db = MagicMock()

    @staticmethod
    def _row(market_id, action, hours_ago):
        """A projection row as the query returns it: (market_id, action, ts)."""
        return (market_id, action, utc_now() - timedelta(hours=hours_ago))

    async def test_open_old_position_is_reported(self):
        self.db.query.return_value = _query_chain(
            [
                self._row("mkt-a", "buy", 200.0),
                self._row("mkt-b", "buy", 300.0),
            ]
        )

        stale = await lifecycle._check_stale_positions(self.db, 3, "0xaaa")

        self.assertEqual({row["market_id"] for row in stale}, {"mkt-a", "mkt-b"})
        self.assertAlmostEqual(
            next(r["age_hours"] for r in stale if r["market_id"] == "mkt-a"),
            200.0,
            delta=1.0,
        )

    async def test_fully_closed_position_is_not_reported(self):
        """Regression: a position sold long ago must not look stale.

        Before netting existed, every historical BUY was reported regardless of
        any subsequent SELL, so a closed position was flagged stale forever.
        """
        self.db.query.return_value = _query_chain(
            [
                self._row("mkt-a", "buy", 500.0),
                self._row("mkt-a", "sell", 400.0),
            ]
        )

        stale = await lifecycle._check_stale_positions(self.db, 3, "0xaaa")

        self.assertEqual(stale, [])

    async def test_partially_closed_position_keeps_remaining_lots(self):
        self.db.query.return_value = _query_chain(
            [
                self._row("mkt-a", "buy", 300.0),
                self._row("mkt-a", "buy", 290.0),
                self._row("mkt-a", "sell", 280.0),
            ]
        )

        stale = await lifecycle._check_stale_positions(self.db, 3, "0xaaa")

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["open_lots"], 1)
        self.assertEqual(stale[0]["open_trades"], 2)
        # The oldest still-open lot drives the age.
        self.assertAlmostEqual(stale[0]["age_hours"], 300.0, delta=1.0)

    async def test_rebuy_after_full_close_starts_a_fresh_lot(self):
        """A market closed and re-entered must be aged from the new entry."""
        self.db.query.return_value = _query_chain(
            [
                self._row("mkt-a", "buy", 900.0),
                self._row("mkt-a", "sell", 800.0),
                self._row("mkt-a", "buy", 2.0),
            ]
        )

        stale = await lifecycle._check_stale_positions(self.db, 168, "0xaaa")

        self.assertEqual(stale, [])

    async def test_recent_open_position_is_not_stale(self):
        self.db.query.return_value = _query_chain([self._row("mkt-a", "buy", 1.0)])

        self.assertEqual(await lifecycle._check_stale_positions(self.db, 1, "0xaaa"), [])

    async def test_threshold_boundary(self):
        """Just inside the window is not stale; just outside it is."""
        threshold = 168
        # Comfortably inside the window.
        self.db.query.return_value = _query_chain(
            [("mkt-a", "buy", utc_now() - timedelta(hours=threshold - 1))]
        )
        self.assertEqual(await lifecycle._check_stale_positions(self.db, 1, "0xaaa", threshold), [])
        # Comfortably outside the window.
        self.db.query.return_value = _query_chain(
            [("mkt-a", "buy", utc_now() - timedelta(hours=threshold + 1))]
        )
        self.assertEqual(
            len(await lifecycle._check_stale_positions(self.db, 1, "0xaaa", threshold)), 1
        )

    async def test_rows_without_execution_time_are_skipped(self):
        self.db.query.return_value = _query_chain(
            [("mkt-a", "buy", utc_now() - timedelta(hours=300)), ("mkt-b", "buy", None)]
        )

        stale = await lifecycle._check_stale_positions(self.db, 3, "0xaaa")

        self.assertEqual([r["market_id"] for r in stale], ["mkt-a"])

    async def test_query_selects_market_action_and_timestamp(self):
        chain = _query_chain([])
        self.db.query.return_value = chain

        await lifecycle._check_stale_positions(self.db, 42, "0xaaa")

        self.db.query.assert_called_once_with(
            UserTrade.market_id, UserTrade.action, UserTrade.executed_at
        )
        # Both sides must be considered, otherwise sells cannot net buys.
        filters = chain.filter.call_args.args
        self.assertIn("user_id", str(filters[0]))
        self.assertIn("IN", str(filters[1]))
        chain.order_by.assert_called_once()

    async def test_empty_result_returns_empty_list(self):
        self.db.query.return_value = _query_chain([])
        self.assertEqual(await lifecycle._check_stale_positions(self.db, 1, "0xaaa"), [])

    async def test_query_failure_is_logged_and_returns_empty_list(self):
        self.db.query.side_effect = RuntimeError("table missing")
        logger_mock = MagicMock()

        with patch.object(lifecycle, "logger", new=logger_mock):
            stale = await lifecycle._check_stale_positions(self.db, 9, "0xaaa")

        self.assertEqual(stale, [])
        self.assertEqual(
            logger_mock.error.call_args.args[0], "Error checking stale positions for user %d: %s"
        )
        self.assertEqual(logger_mock.error.call_args.args[1], 9)


class MergeDuplicatePositionsTests(unittest.IsolatedAsyncioTestCase):
    """``_merge_duplicate_positions`` duplicate detection and averaging."""

    def setUp(self):
        self.db = MagicMock()

    async def test_duplicate_markets_get_weighted_average_entry_price(self):
        dupes = [
            SimpleNamespace(
                market_id="mkt-a",
                count=2,
                total_cost=14.0,  # 10 * 0.2 + 30 * 0.4
                total_amount=40.0,
            ),
            SimpleNamespace(market_id="mkt-b", count=3, total_cost=9.0, total_amount=30.0),
        ]
        chain = _query_chain(dupes)
        self.db.query.return_value = chain

        merged = await lifecycle._merge_duplicate_positions(self.db, 6)

        self.assertEqual(
            merged,
            [
                {
                    "market_id": "mkt-a",
                    "trade_count": 2,
                    "total_amount": 40.0,
                    "avg_entry_price": 0.35,
                    "total_cost": 14.0,
                },
                {
                    "market_id": "mkt-b",
                    "trade_count": 3,
                    "total_amount": 30.0,
                    "avg_entry_price": 0.3,
                    "total_cost": 9.0,
                },
            ],
        )

    async def test_zero_total_amount_yields_zero_average_price(self):
        chain = _query_chain(
            [SimpleNamespace(market_id="mkt-x", count=2, total_cost=0.0, total_amount=0.0)]
        )
        self.db.query.return_value = chain

        merged = await lifecycle._merge_duplicate_positions(self.db, 1)

        self.assertEqual(merged[0]["avg_entry_price"], 0)
        self.assertEqual(merged[0]["total_amount"], 0.0)

    async def test_query_groups_by_market_with_more_than_one_buy(self):
        chain = _query_chain([])
        self.db.query.return_value = chain

        await lifecycle._merge_duplicate_positions(self.db, 12)

        # Four aggregate columns: market_id, count, total_cost, total_amount.
        self.assertEqual(len(self.db.query.call_args.args), 4)
        conditions = chain.filter.call_args.args
        self.assertEqual(len(conditions), 2)
        self.assertEqual(str(conditions[0]), "user_trades.user_id = :user_id_1")
        self.assertEqual(str(conditions[1]), "user_trades.action = :action_1")
        self.assertEqual(conditions[0].right.value, 12)
        chain.group_by.assert_called_once()
        self.assertEqual(str(chain.having.call_args.args[0]), "count(user_trades.id) > :count_1")

    async def test_no_duplicates_returns_empty_list(self):
        self.db.query.return_value = _query_chain([])

        merged = await lifecycle._merge_duplicate_positions(self.db, 1)

        self.assertEqual(merged, [])

    async def test_query_failure_is_logged_and_returns_empty_list(self):
        self.db.query.side_effect = RuntimeError("aggregate unsupported")
        logger_mock = MagicMock()

        with patch.object(lifecycle, "logger", new=logger_mock):
            merged = await lifecycle._merge_duplicate_positions(self.db, 8)

        self.assertEqual(merged, [])
        self.assertEqual(
            logger_mock.error.call_args.args[0],
            "Error detecting duplicate positions for user %d: %s",
        )
        self.assertEqual(logger_mock.error.call_args.args[1], 8)


class RunLifecycleCheckTests(unittest.IsolatedAsyncioTestCase):
    """``run_lifecycle_check`` combined summary and session handling."""

    def setUp(self):
        self.db = MagicMock()

    async def test_summary_combines_all_three_checks(self):
        resolved_mock = AsyncMock(return_value=[{"condition_id": "c1"}])
        stale_mock = AsyncMock(return_value=[{"trade_id": 1}, {"trade_id": 2}])
        duplicates_mock = AsyncMock(return_value=[{"market_id": "m1"}])

        with (
            patch.object(lifecycle, "SessionLocal", return_value=self.db) as session_mock,
            patch.object(lifecycle, "_check_resolved_markets", new=resolved_mock),
            patch.object(lifecycle, "_check_stale_positions", new=stale_mock),
            patch.object(lifecycle, "_merge_duplicate_positions", new=duplicates_mock),
        ):
            result = await lifecycle.run_lifecycle_check(4, "0xaaa")

        session_mock.assert_called_once_with()
        resolved_mock.assert_awaited_once_with(self.db, 4, "0xaaa")
        stale_mock.assert_awaited_once_with(self.db, 4, "0xaaa")
        duplicates_mock.assert_awaited_once_with(self.db, 4)

        self.assertEqual(result["user_id"], 4)
        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["stale_count"], 2)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["resolved_positions"], [{"condition_id": "c1"}])
        self.assertEqual(result["duplicate_positions"], [{"market_id": "m1"}])
        # ``checked_at`` is an ISO-8601 UTC timestamp.
        utc_now().fromisoformat(result["checked_at"])
        self.assertEqual(db_close_count(self.db), 1)

    async def test_all_empty_reports_zero_counts(self):
        with (
            patch.object(lifecycle, "SessionLocal", return_value=self.db),
            patch.object(lifecycle, "_check_resolved_markets", new=AsyncMock(return_value=[])),
            patch.object(lifecycle, "_check_stale_positions", new=AsyncMock(return_value=[])),
            patch.object(lifecycle, "_merge_duplicate_positions", new=AsyncMock(return_value=[])),
        ):
            result = await lifecycle.run_lifecycle_check(1, "0xbbb")

        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(result["stale_count"], 0)
        self.assertEqual(result["duplicate_count"], 0)
        self.assertEqual(result["resolved_positions"], [])
        self.assertEqual(result["stale_positions"], [])
        self.assertEqual(result["duplicate_positions"], [])
        self.assertEqual(db_close_count(self.db), 1)

    async def test_session_is_closed_even_when_a_check_raises(self):
        with (
            patch.object(lifecycle, "SessionLocal", return_value=self.db),
            patch.object(
                lifecycle,
                "_check_resolved_markets",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch.object(lifecycle, "_check_stale_positions", new=AsyncMock(return_value=[])),
            patch.object(lifecycle, "_merge_duplicate_positions", new=AsyncMock(return_value=[])),
            self.assertRaises(RuntimeError) as ctx,
        ):
            await lifecycle.run_lifecycle_check(1, "0xbbb")

        self.assertEqual(str(ctx.exception), "boom")
        self.assertEqual(db_close_count(self.db), 1)


def db_close_count(db):
    """Number of times ``db.close()`` was called."""
    return db.close.call_count


class LifecycleLoopTests(unittest.IsolatedAsyncioTestCase):
    """``_lifecycle_loop`` fan-out, error isolation, and shutdown."""

    async def test_loop_checks_every_user_and_logs_the_summary(self):
        users = [
            SimpleNamespace(id=1, wallet_address="0xaaa"),
            SimpleNamespace(id=2, wallet_address="0xbbb"),
        ]
        db = _session(users)
        check_mock = AsyncMock(
            side_effect=_canned(
                _lifecycle_result(resolved=2, stale=1, duplicates=0),
                _lifecycle_result(),
            )
        )
        sleep_mock = AsyncMock(side_effect=_canned(_LoopBreak))
        logger_mock = MagicMock()

        with (
            patch.object(lifecycle, "SessionLocal", return_value=db),
            patch.object(lifecycle, "run_lifecycle_check", new=check_mock),
            patch.object(lifecycle.asyncio, "sleep", new=sleep_mock),
            patch.object(lifecycle, "logger", new=logger_mock),
            self.assertRaises(_LoopBreak),
        ):
            await lifecycle._lifecycle_loop()

        check_mock.assert_has_awaits([call(1, "0xaaa"), call(2, "0xbbb")])
        self.assertEqual(db_close_count(db), 1)
        sleep_mock.assert_awaited_once_with(lifecycle.LIFECYCLE_CHECK_INTERVAL)

        info_messages = [c.args[0] for c in logger_mock.info.call_args_list]
        self.assertIn("Position lifecycle manager started (interval=%ds)", info_messages)
        self.assertIn(
            "Lifecycle check for user %d: %d resolved, %d stale, %d duplicates", info_messages
        )
        # Only the user with findings produces a summary line.
        self.assertEqual(
            info_messages.count(
                "Lifecycle check for user %d: %d resolved, %d stale, %d duplicates"
            ),
            1,
        )
        summary_call = next(
            c
            for c in logger_mock.info.call_args_list
            if c.args[0].startswith("Lifecycle check for user")
        )
        self.assertEqual(summary_call.args[1:], (1, 2, 1, 0))

    async def test_loop_skips_the_summary_line_when_nothing_is_found(self):
        db = _session([SimpleNamespace(id=1, wallet_address="0xaaa")])
        check_mock = AsyncMock(return_value=_lifecycle_result())
        sleep_mock = AsyncMock(side_effect=_canned(_LoopBreak))
        logger_mock = MagicMock()

        with (
            patch.object(lifecycle, "SessionLocal", return_value=db),
            patch.object(lifecycle, "run_lifecycle_check", new=check_mock),
            patch.object(lifecycle.asyncio, "sleep", new=sleep_mock),
            patch.object(lifecycle, "logger", new=logger_mock),
            self.assertRaises(_LoopBreak),
        ):
            await lifecycle._lifecycle_loop()

        info_messages = [c.args[0] for c in logger_mock.info.call_args_list]
        self.assertNotIn(
            "Lifecycle check for user %d: %d resolved, %d stale, %d duplicates", info_messages
        )
        logger_mock.error.assert_not_called()

    async def test_loop_isolates_a_failing_user_check_and_keeps_going(self):
        users = [
            SimpleNamespace(id=1, wallet_address="0xaaa"),
            SimpleNamespace(id=2, wallet_address="0xbbb"),
        ]
        db = _session(users)
        check_mock = AsyncMock(
            side_effect=_canned(RuntimeError("user 1 exploded"), _lifecycle_result(stale=3))
        )
        sleep_mock = AsyncMock(side_effect=_canned(_LoopBreak))
        logger_mock = MagicMock()

        with (
            patch.object(lifecycle, "SessionLocal", return_value=db),
            patch.object(lifecycle, "run_lifecycle_check", new=check_mock),
            patch.object(lifecycle.asyncio, "sleep", new=sleep_mock),
            patch.object(lifecycle, "logger", new=logger_mock),
            self.assertRaises(_LoopBreak),
        ):
            await lifecycle._lifecycle_loop()

        # The failing user did not stop the loop.
        self.assertEqual(check_mock.await_count, 2)
        self.assertEqual(db_close_count(db), 1)
        error_call = logger_mock.error.call_args_list[0]
        self.assertEqual(error_call.args[0], "Lifecycle check failed for user %d: %s")
        self.assertEqual(error_call.args[1], 1)
        self.assertEqual(str(error_call.args[2]), "user 1 exploded")

    async def test_loop_survives_a_session_error_and_retries_next_cycle(self):
        first_db = _session([SimpleNamespace(id=1, wallet_address="0xaaa")])
        check_mock = AsyncMock(return_value=_lifecycle_result(duplicates=1))
        sleep_mock = AsyncMock(side_effect=_canned(None, _LoopBreak))
        logger_mock = MagicMock()

        with (
            patch.object(
                lifecycle,
                "SessionLocal",
                side_effect=_canned(RuntimeError("db down"), first_db),
            ),
            patch.object(lifecycle, "run_lifecycle_check", new=check_mock),
            patch.object(lifecycle.asyncio, "sleep", new=sleep_mock),
            patch.object(lifecycle, "logger", new=logger_mock),
            self.assertRaises(_LoopBreak),
        ):
            await lifecycle._lifecycle_loop()

        self.assertEqual(
            logger_mock.error.call_args_list[0].args[0], "Position lifecycle loop error: %s"
        )
        self.assertEqual(str(logger_mock.error.call_args_list[0].args[1]), "db down")
        self.assertEqual(check_mock.await_count, 1)
        self.assertEqual(db_close_count(first_db), 1)

    async def test_loop_exits_cleanly_on_cancellation(self):
        sleep_mock = AsyncMock()
        logger_mock = MagicMock()

        with (
            patch.object(lifecycle, "SessionLocal", side_effect=asyncio.CancelledError),
            patch.object(lifecycle.asyncio, "sleep", new=sleep_mock),
            patch.object(lifecycle, "logger", new=logger_mock),
        ):
            # Must return normally rather than propagating CancelledError.
            await lifecycle._lifecycle_loop()

        sleep_mock.assert_not_awaited()
        info_messages = [c.args[0] for c in logger_mock.info.call_args_list]
        self.assertIn("Position lifecycle manager stopped", info_messages)

    async def test_loop_with_no_users_only_queries_and_closes(self):
        db = _session([])
        check_mock = AsyncMock()
        sleep_mock = AsyncMock(side_effect=_canned(_LoopBreak))

        with (
            patch.object(lifecycle, "SessionLocal", return_value=db),
            patch.object(lifecycle, "run_lifecycle_check", new=check_mock),
            patch.object(lifecycle.asyncio, "sleep", new=sleep_mock),
            self.assertRaises(_LoopBreak),
        ):
            await lifecycle._lifecycle_loop()

        check_mock.assert_not_awaited()
        self.assertEqual(db_close_count(db), 1)


class ManagerTaskTests(unittest.IsolatedAsyncioTestCase):
    """``start_position_lifecycle_manager`` / ``stop_position_lifecycle_manager``."""

    async def asyncTearDown(self):
        task = lifecycle._lifecycle_task
        lifecycle._lifecycle_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _start(self):
        loop_mock = AsyncMock(side_effect=_block_forever)
        patcher = patch.object(lifecycle, "_lifecycle_loop", new=loop_mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        await lifecycle.start_position_lifecycle_manager()
        # Let the task actually begin so ``done()`` is meaningful.
        await asyncio.sleep(0)
        return loop_mock, lifecycle._lifecycle_task

    async def test_start_creates_task_and_stop_cancels_it(self):
        loop_mock, task = await self._start()

        self.assertIsNotNone(task)
        self.assertFalse(task.done())
        self.assertEqual(loop_mock.await_count, 1)

        await lifecycle.stop_position_lifecycle_manager()

        self.assertIsNone(lifecycle._lifecycle_task)
        self.assertTrue(task.cancelled())

    async def test_start_is_idempotent_while_task_alive(self):
        loop_mock, first = await self._start()

        await lifecycle.start_position_lifecycle_manager()
        await asyncio.sleep(0)

        self.assertIs(first, lifecycle._lifecycle_task)
        self.assertEqual(loop_mock.await_count, 1)
        await lifecycle.stop_position_lifecycle_manager()

    async def test_start_restarts_after_a_previous_stop(self):
        loop_mock, first = await self._start()
        await lifecycle.stop_position_lifecycle_manager()

        await lifecycle.start_position_lifecycle_manager()
        await asyncio.sleep(0)
        second = lifecycle._lifecycle_task

        self.assertIsNot(first, second)
        self.assertFalse(second.done())
        self.assertEqual(loop_mock.await_count, 2)
        await lifecycle.stop_position_lifecycle_manager()

    async def test_stop_with_no_task_is_a_noop(self):
        lifecycle._lifecycle_task = None

        await lifecycle.stop_position_lifecycle_manager()

        self.assertIsNone(lifecycle._lifecycle_task)

    async def test_stop_does_not_await_an_already_finished_task(self):
        loop_mock, task = await self._start()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        awaits_before = loop_mock.await_count

        await lifecycle.stop_position_lifecycle_manager()

        self.assertTrue(task.cancelled())
        self.assertIsNone(lifecycle._lifecycle_task)
        self.assertEqual(loop_mock.await_count, awaits_before)


class LifecycleMetricsTests(unittest.IsolatedAsyncioTestCase):
    """``get_lifecycle_metrics`` snapshot."""

    async def asyncTearDown(self):
        task = lifecycle._lifecycle_task
        lifecycle._lifecycle_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def test_metrics_report_stopped_when_no_task_exists(self):
        lifecycle._lifecycle_task = None

        self.assertEqual(
            lifecycle.get_lifecycle_metrics(),
            {
                "running": False,
                "check_interval_seconds": lifecycle.LIFECYCLE_CHECK_INTERVAL,
                "stale_threshold_hours": lifecycle.STALE_POSITION_HOURS,
                "auto_redeem_enabled": lifecycle.AUTO_REDEEM_ENABLED,
                "detects_resolved_markets": True,
                "reports_stale_positions": True,
                "reports_duplicate_positions": True,
            },
        )

    async def test_metrics_report_running_while_the_loop_is_alive(self):
        loop_mock = AsyncMock(side_effect=_block_forever)
        with patch.object(lifecycle, "_lifecycle_loop", new=loop_mock):
            await lifecycle.start_position_lifecycle_manager()
            await asyncio.sleep(0)

            metrics = lifecycle.get_lifecycle_metrics()
            self.assertTrue(metrics["running"])

            await lifecycle.stop_position_lifecycle_manager()

        self.assertEqual(lifecycle.LIFECYCLE_CHECK_INTERVAL, 300)
        self.assertEqual(lifecycle.STALE_POSITION_HOURS, 168)
        self.assertTrue(lifecycle.AUTO_REDEEM_ENABLED)

    async def test_metrics_report_stopped_after_the_task_finishes(self):
        loop_mock = AsyncMock(side_effect=_block_forever)
        with patch.object(lifecycle, "_lifecycle_loop", new=loop_mock):
            await lifecycle.start_position_lifecycle_manager()
            task = lifecycle._lifecycle_task
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

            self.assertFalse(lifecycle.get_lifecycle_metrics()["running"])

            await lifecycle.stop_position_lifecycle_manager()


if __name__ == "__main__":
    unittest.main()
