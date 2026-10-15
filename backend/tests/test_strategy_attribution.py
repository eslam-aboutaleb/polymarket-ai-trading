"""Strategy attribution tests (plan 03 Phase A).

Per-strategy P&L is the basis of every loss limit derived from
``UserTrade.pnl``, so a strategy that trades without an attributable
row silently disables its own risk controls. Before this plan:

* the market maker wrote a ``UserTrade`` with no ``strategy_source``,
* the inverse bot wrote only an ``InverseBotAction`` — no ``UserTrade``
  at all, so neither reversal leg reached the FIFO matcher,
* the live stop-loss / take-profit closes never wrote a ``UserTrade``,
  so the sell could not be matched against the buy lot.

A real in-memory SQLite session is used so the ORM interaction and
the aggregation queries are genuinely exercised (same idiom as
``test_execution_analytics.py``). No network or key material is used:
``_place_order_on_polymarket`` is patched in every execution path.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.base import Base
from app.models.stop_loss import StopLossOrder
from app.models.take_profit import TakeProfitOrder
from app.models.user import User
from app.models.user_trade import UserTrade
from app.services.execution_analytics import get_strategy_breakdown
from app.services.stop_loss_monitor import (
    _execute_stop_loss,
    _execute_stop_loss_simulated,
    _execute_take_profit,
    _execute_take_profit_simulated,
)

WALLET = "0xstrategy-attribution-wallet"
USER_ID = 8181
MARKET_ID = "0xmarket-cond"
TOKEN_ID = "0xtoken-1"


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class _AttributionTestBase(unittest.TestCase):
    """Shared in-memory DB with one user."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _trades(self, **filters):
        q = self.db.query(UserTrade).filter(UserTrade.user_id == USER_ID)
        if filters:
            q = q.filter_by(**filters)
        return q.all()

    def _stop_loss(self):
        order = StopLossOrder(
            user_id=USER_ID,
            market_id=MARKET_ID,
            token_id=TOKEN_ID,
            stop_price=0.4,
            size=100.0,
            status="active",
        )
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)
        return order

    def _take_profit(self):
        order = TakeProfitOrder(
            user_id=USER_ID,
            market_id=MARKET_ID,
            token_id=TOKEN_ID,
            take_profit_price=0.9,
            size=100.0,
            status="active",
        )
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)
        return order


class StopLossAttributionTest(_AttributionTestBase):
    """Both the live and paper stop-loss paths leave an attributed row."""

    def test_live_close_records_pending_attributed_trade(self):
        order = self._stop_loss()
        self.db.expire_all()

        async def _run():
            with patch(
                "app.services.stop_loss_monitor._place_order_on_polymarket",
                MagicMock(return_value={"success": True, "order_hash": "0xhash-sl"}),
            ):
                await _execute_stop_loss(self.db, order, "0xpk", None, 0.35)

        import asyncio

        asyncio.run(_run())

        rows = self._trades(strategy_source="stop_loss")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.action, "sell")
        # A submitted order is pending until the trade monitor observes
        # the fill — never "executed" at submission time.
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.order_hash, "0xhash-sl")
        self.assertEqual(row.market_id, MARKET_ID)
        self.assertEqual(row.token_id, TOKEN_ID)
        self.assertEqual(row.expected_price, 0.35)
        self.assertEqual(row.expected_size, 100.0)

    def test_failed_close_records_failed_attributed_trade(self):
        order = self._stop_loss()
        self.db.expire_all()

        async def _run():
            with patch(
                "app.services.stop_loss_monitor._place_order_on_polymarket",
                MagicMock(return_value={"success": False, "error": "book empty"}),
            ):
                await _execute_stop_loss(self.db, order, "0xpk", None, 0.35)

        import asyncio

        asyncio.run(_run())

        rows = self._trades(strategy_source="stop_loss")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "failed")

    def test_paper_close_records_simulated_attributed_trade(self):
        order = self._stop_loss()
        self.db.expire_all()

        import asyncio

        asyncio.run(_execute_stop_loss_simulated(self.db, order, 0.35))

        rows = self._trades(strategy_source="stop_loss")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "simulated")
        self.assertEqual(row.action, "sell")
        self.assertIsNotNone(row.expected_price)
        self.assertIsNotNone(row.expected_size)


class TakeProfitAttributionTest(_AttributionTestBase):
    """Both the live and paper take-profit paths leave an attributed row."""

    def test_live_close_records_pending_attributed_trade(self):
        order = self._take_profit()
        self.db.expire_all()

        async def _run():
            with patch(
                "app.services.stop_loss_monitor._place_order_on_polymarket",
                MagicMock(return_value={"success": True, "order_hash": "0xhash-tp"}),
            ):
                await _execute_take_profit(self.db, order, "0xpk", None, 0.95)

        import asyncio

        asyncio.run(_run())

        rows = self._trades(strategy_source="take_profit")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.order_hash, "0xhash-tp")
        self.assertEqual(row.action, "sell")

    def test_paper_close_records_simulated_attributed_trade(self):
        order = self._take_profit()
        self.db.expire_all()

        import asyncio

        asyncio.run(_execute_take_profit_simulated(self.db, order, 0.95))

        rows = self._trades(strategy_source="take_profit")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "simulated")


class InverseBotAttributionTest(_AttributionTestBase):
    """A reversal records both legs as attributed trades (plan 03 Phase A)."""

    def _row(self):
        from app.models.inverse_bot_position import InverseBotPosition

        row = InverseBotPosition(
            id=11,
            user_id=USER_ID,
            token_id=TOKEN_ID,
            condition_id=MARKET_ID,
            market_title="Will it rain?",
            outcome="Yes",
            enabled=True,
            status="active",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _reversal(self, place_side_effect):
        """Run ``_execute_reversal`` then record its legs, as the caller does."""
        import app.services.inverse_bot_monitor as monitor

        row = self._row()
        settings = SimpleNamespace(
            inverse_bot_default_size_mode="full_notional",
            inverse_bot_fixed_amount=50.0,
        )
        with (
            patch.object(monitor, "_place_order_on_polymarket", place_side_effect),
            patch.object(monitor, "_best_ask_for_token", lambda _token: 0.6),
        ):
            execution = monitor._execute_reversal(
                row=row,
                settings=settings,
                stored_creds={"private_key": "0xpk", "clob_creds": None},
                from_size=100.0,
                from_price=0.5,
                to_token_id="0xtoken-alt",
                to_outcome="No",
                to_price=0.6,
                confidence=80.0,
                recommendation="reverse",
            )
        monitor._record_reversal_trades(
            self.db,
            user_id=USER_ID,
            condition_id=row.condition_id,
            from_token_id=row.token_id,
            from_size=100.0,
            execution=execution,
            to_token_id="0xtoken-alt",
            buy_notional=execution.get("buy_notional"),
        )
        self.db.commit()
        return execution

    def test_reversal_trades_are_recorded_for_both_legs(self):
        execution = self._reversal(
            lambda **kwargs: {"success": True, "order_hash": "0x" + kwargs["side"].lower()}
        )

        self.assertEqual(execution["status"], "success")
        self.assertIn("sell_price", execution)
        self.assertIn("buy_price", execution)

        rows = {r.action: r for r in self._trades(strategy_source="inverse")}
        self.assertEqual(sorted(rows), ["buy", "sell"])
        for action, row in rows.items():
            self.assertEqual(row.status, "pending", action)
            self.assertEqual(row.expected_price, execution[f"{action}_price"], action)
        self.assertEqual(rows["sell"].order_hash, "0xsell")
        self.assertEqual(rows["buy"].order_hash, "0xbuy")

    def test_sell_only_records_failed_buy_leg(self):
        """A buy that fails after the sell still attributes both legs.

        The sell reached the book (pending) and the buy did not
        (failed), which is exactly the case the old InverseBotAction-only
        record could not express.
        """

        def _place(**kwargs):
            if kwargs["side"] == "SELL":
                return {"success": True, "order_hash": "0xsell"}
            return {"success": False, "error": "insufficient balance"}

        execution = self._reversal(_place)

        self.assertEqual(execution["status"], "sell_only")
        rows = {r.action: r for r in self._trades(strategy_source="inverse")}
        self.assertEqual(rows["sell"].status, "pending")
        self.assertEqual(rows["buy"].status, "failed")

    def test_sell_failure_records_no_buy_leg(self):
        execution = self._reversal(lambda **kwargs: {"success": False, "error": "sell rejected"})

        self.assertEqual(execution["status"], "failed")
        rows = self._trades(strategy_source="inverse")
        self.assertEqual([r.status for r in rows], ["failed"])
        self.assertEqual([r.action for r in rows], ["sell"])

    def test_paper_reversal_records_simulated_legs(self):
        """Paper reversals are attributed too, so paper P&L is measurable."""
        import app.services.inverse_bot_monitor as monitor

        row = self._row()
        settings = SimpleNamespace(
            inverse_bot_default_size_mode="full_notional",
            inverse_bot_fixed_amount=50.0,
        )
        execution = monitor._simulate_reversal(
            row=row,
            settings=settings,
            from_size=100.0,
            from_price=0.5,
            to_token_id="0xtoken-alt",
            to_outcome="No",
            to_price=0.6,
        )
        self.assertTrue(execution["simulated"])
        monitor._record_reversal_trades(
            self.db,
            user_id=USER_ID,
            condition_id=row.condition_id,
            from_token_id=row.token_id,
            from_size=100.0,
            execution=execution,
            to_token_id="0xtoken-alt",
            buy_notional=execution.get("buy_notional"),
        )
        self.db.commit()

        rows = self._trades(strategy_source="inverse")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.status for r in rows}, {"simulated"})


class StrategyBreakdownTest(_AttributionTestBase):
    """``get_strategy_breakdown`` exposes the attribution gap."""

    def test_breakdown_orders_by_net_pnl_and_counts_unattributed(self):
        now = datetime.now(UTC)
        rows = [
            UserTrade(
                user_id=USER_ID,
                market_id=MARKET_ID,
                action="buy",
                amount=10.0,
                price=0.4,
                status="executed",
                strategy_source="copy",
                pnl=5.0,
                created_at=now - timedelta(days=1),
            ),
            UserTrade(
                user_id=USER_ID,
                market_id=MARKET_ID,
                action="sell",
                amount=10.0,
                price=0.5,
                status="simulated",
                strategy_source="inverse",
                pnl=-20.0,
                created_at=now - timedelta(days=1),
            ),
            # Legacy row: no strategy source at all.
            UserTrade(
                user_id=USER_ID,
                market_id=MARKET_ID,
                action="buy",
                amount=1.0,
                price=0.5,
                status="executed",
                strategy_source=None,
                created_at=now - timedelta(days=2),
            ),
            # Out of window: must not be counted.
            UserTrade(
                user_id=USER_ID,
                market_id=MARKET_ID,
                action="buy",
                amount=1.0,
                price=0.5,
                status="executed",
                strategy_source="copy",
                pnl=99.0,
                created_at=now - timedelta(days=40),
            ),
        ]
        self.db.add_all(rows)
        self.db.commit()

        payload = get_strategy_breakdown(self.db, USER_ID, window_days=30)

        strategies = payload["strategies"]
        self.assertEqual([s["strategy"] for s in strategies], ["copy", "inverse"])
        # Best net P&L first.
        self.assertGreaterEqual(
            strategies[0]["net_realized_pnl"], strategies[1]["net_realized_pnl"]
        )
        copy = strategies[0]
        self.assertEqual(copy["closed_trades"], 1)
        self.assertEqual(copy["wins"], 1)
        self.assertEqual(copy["win_rate"], 1.0)
        self.assertEqual(copy["gross_realized_pnl"], 5.0)
        inverse = strategies[1]
        self.assertEqual(inverse["losses"], 1)
        self.assertEqual(inverse["net_realized_pnl"], -20.0)
        self.assertEqual(payload["data_quality"]["unattributed_trades"], 1)
        self.assertEqual(payload["data_quality"]["strategies"], 2)

    def test_excludes_other_users(self):
        self.db.add(User(id=999999, wallet_address="0xother"))
        self.db.add(
            UserTrade(
                user_id=999999,
                market_id=MARKET_ID,
                action="buy",
                amount=1.0,
                price=0.5,
                status="executed",
                strategy_source="copy",
                pnl=1000.0,
                created_at=datetime.now(UTC),
            )
        )
        self.db.commit()

        payload = get_strategy_breakdown(self.db, USER_ID, window_days=30)
        self.assertEqual(payload["strategies"], [])


class EmergencyStopAttributionTest(_AttributionTestBase):
    """Emergency-stop panic sells are attributed too (master plan 5.4).

    Emergency stop must cover every writer path. Without an attributed
    row the position's realized P&L stays at zero, which understates the
    loss that triggered the stop and corrupts the drawdown math.
    """

    def test_panic_sells_are_recorded_as_emergency(self):
        from unittest.mock import AsyncMock, patch

        from app.api.routes.trades import emergency_stop

        position = {
            "asset_id": TOKEN_ID,
            "condition_id": MARKET_ID,
            "size": 50.0,
            "curPrice": 0.3,
        }
        service = AsyncMock()
        service.get_positions = AsyncMock(return_value=[position])

        async def _run():
            with (
                patch(
                    "app.api.routes.trades.load_wallet_credentials",
                    return_value={
                        "private_key": "0xpk",
                        "clob_creds": {"api_key": "k"},
                    },
                ),
                patch("app.api.routes.trades.get_polymarket_service", return_value=service),
                patch(
                    "app.api.routes.trades._place_order_on_polymarket",
                    MagicMock(return_value={"success": True, "order_hash": "0xemerg"}),
                ),
            ):
                return await emergency_stop(
                    close_positions=True,
                    current_user={"user_id": USER_ID},
                    db=self.db,
                )

        import asyncio

        result = asyncio.run(_run())

        self.assertTrue(result.halted)
        self.assertEqual(result.positions_closed, 1)
        rows = self._trades(strategy_source="emergency")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.action, "sell")
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.order_hash, "0xemerg")
        self.assertEqual(row.token_id, TOKEN_ID)
        self.assertEqual(row.market_id, MARKET_ID)


if __name__ == "__main__":
    unittest.main()
