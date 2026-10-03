"""Unit tests for realized-PnL reconciliation.

Every loss circuit breaker in the bot reads ``UserTrade.pnl``, and nothing else
in the codebase writes it. These tests therefore cover the mechanism the entire
risk system depends on.

A real in-memory SQLite session is used (via SQLAlchemy) so the ORM interaction
and the commit are genuinely exercised rather than mocked.
"""

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.user_trade import UserTrade
from app.services.pnl_reconciliation import (
    has_pnl_history,
    reconcile_realized_pnl,
    reconciliation_summary,
    total_realized_pnl,
    unreconciled_sell_count,
)

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class PnlReconciliationTestBase(unittest.TestCase):
    """Shared in-memory database with the UserTrade table created."""

    def setUp(self):
        self.engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(self.engine, tables=[UserTrade.__table__])
        self.db = sessionmaker(bind=self.engine, future=True)()
        # addCleanup is LIFO: register the engine first so the session closes
        # before the engine is disposed.
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self._next_id = 1

    def _trade(self, action, market_id="mkt-a", price=0.5, amount=50.0, **kwargs):
        """Insert one UserTrade and return it."""
        trade = UserTrade(
            user_id=kwargs.pop("user_id", 1),
            market_id=market_id,
            action=action,
            amount=amount,
            price=price,
            status=kwargs.pop("status", "executed"),
            executed_at=kwargs.pop("executed_at", BASE_TIME),
            **kwargs,
        )
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        self._next_id += 1
        return trade

    def _buy(self, market_id="mkt-a", price=0.5, amount=50.0, **kw):
        return self._trade("buy", market_id, price, amount, **kw)

    def _sell(self, market_id="mkt-a", price=0.5, amount=50.0, **kw):
        return self._trade("sell", market_id, amount=amount, price=price, **kw)


class SimpleCloseTests(PnlReconciliationTestBase):
    """A single buy closed by a single sell."""

    def test_profitable_close_records_positive_pnl(self):
        # 100 shares at 0.50, sold at 0.70 => +20
        self._buy(price=0.5, amount=50.0)
        sell = self._sell(price=0.7, amount=70.0)

        result = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(result.sells_processed, 1)
        self.assertAlmostEqual(result.realized_pnl, 20.0, places=4)
        self.db.refresh(sell)
        self.assertAlmostEqual(sell.pnl, 20.0, places=4)

    def test_losing_close_records_negative_pnl(self):
        # 100 shares at 0.50, sold at 0.30 => -20
        self._buy(price=0.5, amount=50.0)
        sell = self._sell(price=0.3, amount=30.0)

        reconcile_realized_pnl(self.db, 1)

        self.db.refresh(sell)
        self.assertAlmostEqual(sell.pnl, -20.0, places=4)

    def test_break_even_close_is_zero(self):
        self._buy(price=0.5, amount=50.0)
        sell = self._sell(price=0.5, amount=50.0)

        reconcile_realized_pnl(self.db, 1)

        self.db.refresh(sell)
        self.assertAlmostEqual(sell.pnl, 0.0, places=4)

    def test_open_position_records_no_pnl(self):
        self._buy(price=0.5, amount=50.0)

        result = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(result.sells_processed, 0)
        self.assertEqual(result.open_lots_remaining, 1)
        self.assertFalse(has_pnl_history(self.db, 1))

    def test_no_trades_at_all(self):
        result = reconcile_realized_pnl(self.db, 99)

        self.assertEqual(result.sells_processed, 0)
        self.assertEqual(result.warnings, [])
        self.assertEqual(total_realized_pnl(self.db, 99), 0.0)


class FifoMatchingTests(PnlReconciliationTestBase):
    """Sells consume the oldest open lots first."""

    def test_consumes_oldest_lot_first(self):
        # Older buy at 0.20, newer at 0.80. Sell 50 shares' worth at 0.50.
        self._buy(price=0.2, amount=20.0, executed_at=BASE_TIME)
        self._buy(price=0.8, amount=80.0, executed_at=BASE_TIME + timedelta(hours=1))
        sell = self._sell(price=0.5, amount=50.0, executed_at=BASE_TIME + timedelta(hours=2))

        result = reconcile_realized_pnl(self.db, 1)

        # 100 shares bought at 0.20 (cost 20), sold 100 shares (notional 50) at
        # 0.50 => 100 * (0.50 - 0.20) = 30
        self.db.refresh(sell)
        self.assertAlmostEqual(sell.pnl, 30.0, places=4)
        self.assertEqual(result.lots_consumed, 1)

    def test_sell_spanning_two_lots_allocates_pro_rata(self):
        self._buy(price=0.2, amount=20.0, executed_at=BASE_TIME)  # 100 sh @ 0.20
        self._buy(
            price=0.8, amount=80.0, executed_at=BASE_TIME + timedelta(hours=1)
        )  # 100 sh @ 0.80
        sell = self._sell(price=0.5, amount=100.0, executed_at=BASE_TIME + timedelta(hours=2))

        result = reconcile_realized_pnl(self.db, 1)

        # 100 sh from lot1: (0.5-0.2)*100 = 30
        # 100 sh from lot2: (0.5-0.8)*100 = -30
        self.db.refresh(sell)
        self.assertAlmostEqual(sell.pnl, 0.0, places=4)
        self.assertEqual(result.lots_consumed, 2)

    def test_partially_closed_lot_is_not_reused(self):
        self._buy(price=0.5, amount=50.0)  # 100 shares
        self._sell(price=0.6, amount=30.0, executed_at=BASE_TIME + timedelta(hours=1))  # 50 shares
        second = self._sell(price=0.4, amount=20.0, executed_at=BASE_TIME + timedelta(hours=2))

        reconcile_realized_pnl(self.db, 1)

        self.db.refresh(second)
        # Only 50 shares remained: (0.4 - 0.5) * 50 = -5
        self.assertAlmostEqual(second.pnl, -5.0, places=4)

    def test_rebuy_after_full_close_gets_a_fresh_lot(self):
        self._buy(price=0.5, amount=50.0)  # 100 sh @ 0.50
        self._sell(price=0.5, amount=50.0, executed_at=BASE_TIME + timedelta(hours=1))
        self._buy(
            price=0.8, amount=80.0, executed_at=BASE_TIME + timedelta(hours=2)
        )  # 100 sh @ 0.80
        sell = self._sell(price=1.0 - 0.0, amount=100.0, executed_at=BASE_TIME + timedelta(hours=3))

        reconcile_realized_pnl(self.db, 1)

        self.db.refresh(sell)
        # The second sell must use the 0.80 lot, not the exhausted 0.50 lot.
        self.assertAlmostEqual(sell.pnl, 20.0, places=4)


class IdempotencyTests(PnlReconciliationTestBase):
    """Re-running the reconciler must not double-count."""

    def test_second_run_is_a_no_op(self):
        self._buy(price=0.5, amount=50.0)
        self._sell(price=0.7, amount=70.0)

        first = reconcile_realized_pnl(self.db, 1)
        second = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(first.sells_processed, 1)
        self.assertEqual(second.sells_processed, 0)
        self.assertAlmostEqual(second.realized_pnl, 0.0)
        self.assertAlmostEqual(total_realized_pnl(self.db, 1), 20.0, places=4)

    def test_pnl_survives_repeated_runs(self):
        self._buy(price=0.5, amount=50.0)
        self._sell(price=0.7, amount=70.0)

        for _ in range(5):
            reconcile_realized_pnl(self.db, 1)

        self.assertAlmostEqual(total_realized_pnl(self.db, 1), 20.0, places=4)


class StatusFilteringTests(PnlReconciliationTestBase):
    """Only trades that reached the exchange may contribute PnL."""

    def test_failed_sell_is_ignored(self):
        self._buy(price=0.5, amount=50.0)
        self._sell(price=0.9, amount=90.0, status="failed")

        result = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(result.sells_processed, 0)
        self.assertEqual(result.skipped, 1)
        self.assertAlmostEqual(total_realized_pnl(self.db, 1), 0.0)

    def test_pending_buy_does_not_open_a_lot(self):
        self._buy(price=0.5, amount=50.0, status="pending")
        sell = self._sell(price=0.5, amount=50.0)

        reconcile_realized_pnl(self.db, 1)

        self.db.refresh(sell)
        self.assertIsNone(sell.pnl)
        self.assertGreater(result_unmatched(self.db), 0)

    def test_cancelled_trade_is_ignored(self):
        self._buy(price=0.5, amount=50.0, status="cancelled")
        self._sell(price=0.5, amount=50.0)

        reconcile_realized_pnl(self.db, 1)

        self.assertFalse(has_pnl_history(self.db, 1))


class MalformedInputTests(PnlReconciliationTestBase):
    """Bad rows must be skipped, never crash the risk system."""

    def test_zero_price_buy_is_skipped(self):
        self._buy(price=0.0, amount=50.0)
        sell = self._sell(price=0.5, amount=50.0)

        result = reconcile_realized_pnl(self.db, 1)

        self.db.refresh(sell)
        self.assertIsNone(sell.pnl)
        self.assertTrue(result.warnings)

    def test_negative_amount_is_skipped(self):
        self._buy(price=0.5, amount=-50.0)
        result = reconcile_realized_pnl(self.db, 1)
        self.assertTrue(result.warnings)

    def test_missing_execution_time_is_filtered_out(self):
        """Rows with no execution time are excluded by the SQL filter.

        They never reach the matching loop, so they cannot open a lot and
        cannot be reported as a processing skip.
        """
        self._buy(price=0.5, amount=50.0, executed_at=None)
        sell = self._sell(price=0.6, amount=60.0)

        result = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(result.sells_processed, 0)
        self.assertEqual(result.open_lots_remaining, 0)
        self.db.refresh(sell)
        self.assertIsNone(sell.pnl)

    def test_sell_without_any_buy_is_reported_not_silently_zero(self):
        self._sell(price=0.5, amount=50.0)

        result = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(result.sells_processed, 0)
        self.assertAlmostEqual(result.unmatched_sell_shares, 100.0, places=4)
        self.assertTrue(any("no open buy lot" in w for w in result.warnings))

    def test_oversized_sell_flags_the_excess(self):
        self._buy(price=0.5, amount=50.0)  # 100 shares
        sell = self._sell(price=0.6, amount=100.0)  # 166.67 shares

        result = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(result.sells_processed, 1)
        self.assertGreater(result.unmatched_sell_shares, 0)
        self.db.refresh(sell)
        # The matched portion still produced PnL.
        self.assertAlmostEqual(sell.pnl, 10.0, places=4)


class MultiUserTests(PnlReconciliationTestBase):
    """Users must not see each other's trades."""

    def test_reconciliation_is_scoped_to_one_user(self):
        self._buy(price=0.5, amount=50.0, user_id=1)
        self._buy(price=0.5, amount=50.0, user_id=2)
        self._sell(price=0.7, amount=70.0, user_id=2)

        result = reconcile_realized_pnl(self.db, 1)

        self.assertEqual(result.sells_processed, 0)
        self.assertFalse(has_pnl_history(self.db, 1))
        self.assertTrue(has_pnl_history(self.db, 2) is False)  # not yet reconciled

    def test_second_user_reconciled_independently(self):
        self._buy(price=0.5, amount=50.0, user_id=1)
        self._buy(price=0.5, amount=50.0, user_id=2)
        self._sell(price=0.7, amount=70.0, user_id=2)

        reconcile_realized_pnl(self.db, 2)

        self.assertAlmostEqual(total_realized_pnl(self.db, 2), 20.0, places=4)
        self.assertAlmostEqual(total_realized_pnl(self.db, 1), 0.0)


class HealthQueryTests(PnlReconciliationTestBase):
    """The helpers the risk gates and admin views depend on."""

    def test_has_pnl_history_distinguishes_no_data_from_no_loss(self):
        self.assertFalse(has_pnl_history(self.db, 1))

        self._buy(price=0.5, amount=50.0)
        self._sell(price=0.5, amount=50.0)
        reconcile_realized_pnl(self.db, 1)

        # Zero PnL, but data exists.
        self.assertAlmostEqual(total_realized_pnl(self.db, 1), 0.0)
        self.assertTrue(has_pnl_history(self.db, 1))

    def test_unreconciled_sell_count(self):
        self._buy(price=0.5, amount=50.0)
        self._sell(price=0.6, amount=60.0)

        self.assertEqual(unreconciled_sell_count(self.db, 1), 1)

        reconcile_realized_pnl(self.db, 1)
        self.assertEqual(unreconciled_sell_count(self.db, 1), 0)

    def test_summary_reports_every_field(self):
        self._buy(price=0.5, amount=50.0)
        self._sell(price=0.7, amount=70.0)
        reconcile_realized_pnl(self.db, 1)

        summary = reconciliation_summary(self.db, 1)

        self.assertEqual(summary["user_id"], 1)
        self.assertAlmostEqual(summary["total_realized_pnl"], 20.0, places=4)
        self.assertTrue(summary["has_pnl_history"])
        self.assertEqual(summary["unreconciled_sells"], 0)


class MultiSellTests(PnlReconciliationTestBase):
    """Several partial exits against one lot."""

    def test_three_partial_exits_accumulate(self):
        """Each exit has the same USDC notional, so fills differ in share count."""
        self._buy(price=0.4, amount=40.0)  # 100 shares @ 0.40
        for i, price in enumerate([0.5, 0.6, 0.8], start=1):
            self._sell(
                price=price,
                amount=20.0,
                executed_at=BASE_TIME + timedelta(hours=i),
            )

        reconcile_realized_pnl(self.db, 1)

        # 20 USDC notional => 40, 33.33 and 25 shares at 0.5, 0.6 and 0.8.
        expected = (20 / 0.5) * 0.1 + (20 / 0.6) * 0.2 + (20 / 0.8) * 0.4
        self.assertAlmostEqual(total_realized_pnl(self.db, 1), expected, places=4)


def result_unmatched(db) -> float:
    """Total unmatched sell shares recorded for user 1."""
    sells = db.query(UserTrade).filter(UserTrade.user_id == 1, UserTrade.action == "sell").all()
    return sum(float(s.amount) / float(s.price) for s in sells if s.pnl is None)


if __name__ == "__main__":
    unittest.main()
