"""Order idempotency tests (plan 01, F2).

A replayed Idempotency-Key must return the stored result
and never place a second order on the exchange — for the
manual execute endpoint and for the copy-trade path, whose
key is derived from the source trade identity.  The result
must survive cache loss via the DB fallback table.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.api.routes.trades import ExecuteTradeRequest, execute_trade
from app.models.base import Base
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.services import copy_trade_service
from app.services.pre_trade_gate import (
    OrderIdempotencyKey,  # noqa: F401
    _get_idempotency_cache,
)

WALLET = "0xidempotency-wallet"
USER_ID = 5151
TRADER_WALLET = "0xtrader-wallet"


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=USER_ID, wallet_address=WALLET))
    db.add(
        UserSettings(
            user_id=USER_ID,
            copy_trading_enabled=True,
            max_position_size=100.0,
            daily_loss_limit=500.0,
        )
    )
    db.commit()
    return db


def _current_user() -> dict:
    return {"user_id": USER_ID, "wallet_address": WALLET, "is_admin": False}


def _body() -> ExecuteTradeRequest:
    return ExecuteTradeRequest(
        token_id="0xtoken",
        market_id="0xmarket",
        side="BUY",
        price=0.5,
        size=50.0,
    )


ORDER_RESULT = {
    "success": True,
    "order_hash": "0xorder-1",
    "status": "submitted",
}


class ManualExecuteIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """A replayed key replays the stored result."""

    def setUp(self):
        self.db = _make_db()
        _get_idempotency_cache().clear()

    def tearDown(self):
        self.db.close()

    async def _execute(self, key: str | None, place_order: MagicMock | None = None):
        """Run one execute request, optionally against a shared mock."""
        mock = (
            place_order if place_order is not None else MagicMock(return_value=dict(ORDER_RESULT))
        )
        with (
            patch(
                "app.api.routes.trades.load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch("app.api.routes.trades._place_order_on_polymarket", new=mock),
        ):
            return await execute_trade(
                body=_body(),
                idempotency_key=key,
                current_user=_current_user(),
                db=self.db,
            )

    async def test_duplicate_key_returns_original_result(self):
        place_order = MagicMock(return_value=dict(ORDER_RESULT))
        first = await self._execute("key-1", place_order)
        second = await self._execute("key-1", place_order)

        self.assertTrue(first.success)
        self.assertEqual(second.order_hash, first.order_hash)
        self.assertEqual(second.trade_id, first.trade_id)
        # Only the first request reached the exchange.
        place_order.assert_called_once()
        # Only one trade was recorded.
        trades = self.db.query(UserTrade).filter(UserTrade.user_id == USER_ID).all()
        self.assertEqual(len(trades), 1)

    async def test_distinct_keys_place_distinct_orders(self):
        place_order = MagicMock(return_value=dict(ORDER_RESULT))
        first = await self._execute("key-a", place_order)
        second = await self._execute("key-b", place_order)
        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(place_order.call_count, 2)
        self.assertNotEqual(first.trade_id, second.trade_id)

    async def test_result_survives_cache_loss(self):
        place_order = MagicMock(return_value=dict(ORDER_RESULT))
        first = await self._execute("key-durable", place_order)
        # Simulate Redis loss: the shared cache is emptied,
        # so the replay must come from the DB fallback table.
        _get_idempotency_cache().clear()
        second = await self._execute("key-durable", place_order)

        self.assertEqual(second.order_hash, first.order_hash)
        self.assertEqual(second.trade_id, first.trade_id)
        place_order.assert_called_once()
        rows = (
            self.db.query(OrderIdempotencyKey).filter(OrderIdempotencyKey.user_id == USER_ID).all()
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].idempotency_key, "key-durable")


class CopyTradeIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """The copy-trade path is idempotent on the source trade."""

    def setUp(self):
        self.db = _make_db()
        _get_idempotency_cache().clear()

    def tearDown(self):
        self.db.close()

    async def _copy(self, trade_history_id: int, place_order: MagicMock | None = None):
        """Run one copy trade, optionally against a shared mock."""
        plan = {
            "rejection": None,
            "trade_size_pre_safety": 50.0,
            "trade_size_final": 50.0,
            "configured_sizing_mode": "max_position_daily_loss",
            "configured_copy_wallet_mode": "dynamic_main_wallet_percentage",
            "sizing_mode_applied": "max_position_daily_loss",
            "copy_wallet_mode_applied": "dynamic_main_wallet_percentage",
            "trader_trade_notional": 100.0,
            "trader_wallet_balance": 1000.0,
            "copy_wallet_base": 500.0,
            "ratio": 0.5,
        }
        mock = (
            place_order
            if place_order is not None
            else MagicMock(
                return_value={
                    "success": True,
                    "order_hash": "0xcopy-1",
                    "status": "submitted",
                }
            )
        )
        with (
            patch(
                "app.services.copy_trade_service.load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch(
                "app.services.copy_trade_service._plan_copy_trade_size",
                new=AsyncMock(return_value=plan),
            ),
            patch("app.services.copy_trade_service._place_order_on_polymarket", new=mock),
        ):
            return await copy_trade_service.execute_copy_trade(
                db=self.db,
                user_id=USER_ID,
                trader_wallet=TRADER_WALLET,
                market_id="0xmarket",
                token_id="0xtoken",
                side="BUY",
                price=0.5,
                trader_amount=100.0,
                trade_history_id=trade_history_id,
            )

    async def test_same_source_trade_places_one_order(self):
        place_order = MagicMock(
            return_value={
                "success": True,
                "order_hash": "0xcopy-1",
                "status": "submitted",
            }
        )
        first = await self._copy(77, place_order)
        second = await self._copy(77, place_order)

        self.assertTrue(first["executed"])
        self.assertEqual(second["order_hash"], "0xcopy-1")
        self.assertEqual(second["trade_id"], first["trade_id"])
        # A submitted copy order is pending until a fill.
        self.assertEqual(second["status"], "pending")
        place_order.assert_called_once()

    async def test_result_survives_cache_loss(self):
        place_order = MagicMock(
            return_value={
                "success": True,
                "order_hash": "0xcopy-1",
                "status": "submitted",
            }
        )
        first = await self._copy(88, place_order)
        _get_idempotency_cache().clear()
        second = await self._copy(88, place_order)

        self.assertEqual(second["order_hash"], first["order_hash"])
        place_order.assert_called_once()

    async def test_different_source_trades_place_distinct_orders(self):
        place_order = MagicMock(
            return_value={
                "success": True,
                "order_hash": "0xcopy-1",
                "status": "submitted",
            }
        )
        first = await self._copy(90, place_order)
        second = await self._copy(91, place_order)
        self.assertEqual(place_order.call_count, 2)
        self.assertNotEqual(first["trade_id"], second["trade_id"])


if __name__ == "__main__":
    unittest.main()
