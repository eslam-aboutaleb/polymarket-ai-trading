"""Fill-tracking tests (plan 01, F3).

UserTrade.status flows pending → executed | failed | cancelled,
with ``executed_at`` set only when a fill is observed — via
WS user-channel events matched by order_hash, or via the 60s
reconciliation job for pending orders older than 30s.  The
15-minute orphaned-GTC job cancels local-pending orders that
outlived the GTC TTL.
"""

import contextlib
import unittest
from datetime import UTC, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.base import Base
from app.models.user import User
from app.models.user_trade import UserTrade
from app.services import trade_monitor
from app.services.pre_trade_gate import OrderIdempotencyKey  # noqa: F401
from app.utils.time import utc_now

WALLET = "0xfill-tracking-wallet"
USER_ID = 6262
ORDER_HASH = "0xfill-order-hash-0001"


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _pending_trade(db, order_hash=ORDER_HASH, age_seconds=0):
    trade = UserTrade(
        user_id=USER_ID,
        market_id="0xmarket",
        token_id="0xtoken",
        action="buy",
        amount=50.0,
        price=0.5,
        status="pending",
        order_hash=order_hash,
        created_at=utc_now() - timedelta(seconds=age_seconds),
    )
    db.add(trade)
    db.commit()
    return trade


def _second_precision(moment):
    return moment.replace(microsecond=0)


def _as_utc(moment):
    """SQLite DateTime round-trips drop tzinfo; restore it."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


class WsFillMatchingTests(unittest.IsolatedAsyncioTestCase):
    """A WS event carrying an order hash resolves the pending order."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_event_order_hash_extraction(self):
        self.assertEqual(
            trade_monitor._event_order_hash({"order_hash": "0xa"}),
            "0xa",
        )
        self.assertEqual(
            trade_monitor._event_order_hash({"order_id": "0xb"}),
            "0xb",
        )
        self.assertEqual(
            trade_monitor._event_order_hash({"hash": "0xc"}),
            "0xc",
        )
        self.assertIsNone(trade_monitor._event_order_hash({}))

    def test_mark_pending_filled_sets_executed_at_from_fill(self):
        trade = _pending_trade(self.db)
        fill_time = _second_precision(utc_now() - timedelta(seconds=5))
        marked = trade_monitor._mark_pending_trades_filled(
            self.db,
            ORDER_HASH,
            filled_at=fill_time,
        )
        self.assertEqual(marked, 1)
        self.db.refresh(trade)
        self.assertEqual(trade.status, "executed")
        self.assertEqual(_as_utc(trade.executed_at), fill_time)

    def test_mark_pending_filled_ignores_other_orders(self):
        _pending_trade(self.db, order_hash="0xother-order")
        marked = trade_monitor._mark_pending_trades_filled(self.db, ORDER_HASH)
        self.assertEqual(marked, 0)

    async def test_ws_event_marks_pending_filled(self):
        trade = _pending_trade(self.db)
        event = {
            "type": "trade",
            "order_hash": ORDER_HASH,
            "match_time": int(utc_now().timestamp()),
            "user": "0xsomewallet",
        }
        with patch.object(trade_monitor, "SessionLocal", self.SessionFactory):
            await trade_monitor._handle_ws_event(event)
        self.db.refresh(trade)
        self.assertEqual(trade.status, "executed")
        self.assertIsNotNone(trade.executed_at)


class FillReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """The 60s job resolves pending orders against the CLOB."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _client_patches(self, trades):
        fake_client = MagicMock(name="ClobClient")
        return (
            fake_client,
            (
                patch.object(trade_monitor, "SessionLocal", self.SessionFactory),
                patch.object(
                    trade_monitor,
                    "load_wallet_credentials",
                    return_value={"private_key": "0xpk", "clob_creds": None},
                ),
                patch.object(trade_monitor, "_resolve_proxy_address", return_value=None),
                patch.object(trade_monitor, "build_clob_client", return_value=fake_client),
                patch.object(trade_monitor, "clob_trades_for_order", return_value=trades),
            ),
        )

    async def test_reconciliation_marks_filled_orders(self):
        trade = _pending_trade(self.db, age_seconds=60)
        fill_time = _second_precision(utc_now() - timedelta(seconds=10))
        _client, managers = self._client_patches([{"match_time": int(fill_time.timestamp())}])
        with contextlib.ExitStack() as stack:
            for m in managers:
                stack.enter_context(m)
            await trade_monitor._reconcile_pending_fills()
        self.db.refresh(trade)
        self.assertEqual(trade.status, "executed")
        self.assertEqual(_as_utc(trade.executed_at), fill_time)

    async def test_reconciliation_leaves_fresh_orders_pending(self):
        trade = _pending_trade(self.db, age_seconds=5)
        _client, managers = self._client_patches([{"match_time": int(utc_now().timestamp())}])
        with contextlib.ExitStack() as stack:
            for m in managers:
                stack.enter_context(m)
            await trade_monitor._reconcile_pending_fills()
        self.db.refresh(trade)
        self.assertEqual(trade.status, "pending")
        self.assertIsNone(trade.executed_at)

    async def test_reconciliation_leaves_unfilled_orders_pending(self):
        trade = _pending_trade(self.db, age_seconds=60)
        _client, managers = self._client_patches([])
        with contextlib.ExitStack() as stack:
            for m in managers:
                stack.enter_context(m)
            await trade_monitor._reconcile_pending_fills()
        self.db.refresh(trade)
        self.assertEqual(trade.status, "pending")
        self.assertIsNone(trade.executed_at)


class GtcReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """The 15-min job cancels local-pending orders older than the GTC TTL."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _client_patches(self, open_orders, trades):
        fake_client = MagicMock(name="ClobClient")
        fake_client.get_orders.return_value = open_orders
        return (
            fake_client,
            (
                patch.object(trade_monitor, "SessionLocal", self.SessionFactory),
                patch.object(
                    trade_monitor,
                    "load_wallet_credentials",
                    return_value={"private_key": "0xpk", "clob_creds": None},
                ),
                patch.object(trade_monitor, "_resolve_proxy_address", return_value=None),
                patch.object(trade_monitor, "build_clob_client", return_value=fake_client),
                patch.object(trade_monitor, "clob_trades_for_order", return_value=trades),
            ),
        )

    async def test_still_open_order_is_cancelled_on_exchange(self):
        trade = _pending_trade(self.db, age_seconds=86400 * 2)
        fake_client, managers = self._client_patches([{"id": ORDER_HASH}], [])
        with contextlib.ExitStack() as stack:
            for m in managers:
                stack.enter_context(m)
            await trade_monitor._reconcile_orphaned_gtc_orders()
        self.db.refresh(trade)
        self.assertEqual(trade.status, "cancelled")
        fake_client.cancel.assert_called_once_with(ORDER_HASH)

    async def test_filled_order_is_marked_executed(self):
        trade = _pending_trade(self.db, age_seconds=86400 * 2)
        fill_time = _second_precision(utc_now() - timedelta(seconds=30))
        _fake_client, managers = self._client_patches(
            [], [{"match_time": int(fill_time.timestamp())}]
        )
        with contextlib.ExitStack() as stack:
            for m in managers:
                stack.enter_context(m)
            await trade_monitor._reconcile_orphaned_gtc_orders()
        self.db.refresh(trade)
        self.assertEqual(trade.status, "executed")
        self.assertEqual(_as_utc(trade.executed_at), fill_time)

    async def test_vanished_order_is_marked_cancelled(self):
        trade = _pending_trade(self.db, age_seconds=86400 * 2)
        _fake_client, managers = self._client_patches([], [])
        with contextlib.ExitStack() as stack:
            for m in managers:
                stack.enter_context(m)
            await trade_monitor._reconcile_orphaned_gtc_orders()
        self.db.refresh(trade)
        self.assertEqual(trade.status, "cancelled")
        self.assertIsNone(trade.executed_at)


if __name__ == "__main__":
    unittest.main()
