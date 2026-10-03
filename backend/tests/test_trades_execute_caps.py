"""Safety-cap tests for the manual execute endpoint (plan 01, F1).

The manual execute path must run the same pre-trade gate as
copy trading before any order reaches the exchange: an
over-cap order is rejected with 403 naming the violated
limit, and exposure caps count pending (unfilled) notional
so stacked pending orders cannot exceed the position cap.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.api.routes.trades import ExecuteTradeRequest, execute_trade
from app.models.base import Base
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.services.pre_trade_gate import OrderIdempotencyKey  # noqa: F401

WALLET = "0xexecute-cap-wallet"
USER_ID = 4242


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
            copy_trading_enabled=False,
            max_position_size=100.0,
            daily_loss_limit=500.0,
        )
    )
    db.commit()
    return db


def _body(size: float) -> ExecuteTradeRequest:
    return ExecuteTradeRequest(
        token_id="0xtoken",
        market_id="0xmarket",
        side="BUY",
        price=0.5,
        size=size,
    )


def _current_user() -> dict:
    return {"user_id": USER_ID, "wallet_address": WALLET, "is_admin": False}


class ExecuteCapsTests(unittest.IsolatedAsyncioTestCase):
    """The pre-trade gate guards manual execution."""

    def setUp(self):
        self.db = _make_db()

    def tearDown(self):
        self.db.close()

    async def _execute(self, size: float):
        with (
            patch(
                "app.api.routes.trades.load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch(
                "app.api.routes.trades._place_order_on_polymarket",
                return_value={
                    "success": True,
                    "order_hash": "0xorder",
                    "status": "submitted",
                },
            ) as place_order,
        ):
            response = await execute_trade(
                body=_body(size),
                idempotency_key=None,
                current_user=_current_user(),
                db=self.db,
            )
        return response, place_order

    async def test_over_cap_order_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._execute(150.0)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "max_position_size")

    async def test_under_cap_order_is_submitted(self):
        response, place_order = await self._execute(50.0)
        self.assertTrue(response.success)
        self.assertEqual(response.size, 50.0)
        place_order.assert_called_once()
        self.assertEqual(place_order.call_args.kwargs["size"], 50.0)

    async def test_pending_exposure_reduces_the_allowance(self):
        self.db.add(
            UserTrade(
                user_id=USER_ID,
                market_id="0xmarket",
                token_id="0xtoken",
                action="buy",
                amount=60.0,
                price=0.5,
                status="pending",
                order_hash="0xpending",
            )
        )
        self.db.commit()
        response, place_order = await self._execute(50.0)
        self.assertTrue(response.success)
        # 100 cap − 60 pending = 40 remaining allowance.
        self.assertEqual(response.size, 40.0)
        self.assertEqual(place_order.call_args.kwargs["size"], 40.0)

    async def test_pending_exposure_exhausting_the_cap_is_rejected(self):
        self.db.add(
            UserTrade(
                user_id=USER_ID,
                market_id="0xmarket",
                token_id="0xtoken",
                action="buy",
                amount=100.0,
                price=0.5,
                status="pending",
                order_hash="0xpending",
            )
        )
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            await self._execute(50.0)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(
            ctx.exception.detail,
            "Max position size reached (pending exposure)",
        )

    async def test_submitted_order_is_pending_until_fill(self):
        response, _place_order = await self._execute(50.0)
        self.assertEqual(response.status, "submitted")
        trade = self.db.query(UserTrade).filter(UserTrade.user_id == USER_ID).first()
        self.assertIsNotNone(trade)
        self.assertEqual(trade.status, "pending")
        self.assertIsNone(trade.executed_at)
        self.assertEqual(trade.token_id, "0xtoken")

    async def test_failed_submission_is_recorded_failed(self):
        with (
            patch(
                "app.api.routes.trades.load_wallet_credentials",
                return_value={"private_key": "0xpk", "clob_creds": None},
            ),
            patch(
                "app.api.routes.trades._place_order_on_polymarket",
                return_value={
                    "success": False,
                    "order_hash": None,
                    "status": "failed",
                    "error": "exchange rejected",
                },
            ),
        ):
            response = await execute_trade(
                body=_body(50.0),
                idempotency_key=None,
                current_user=_current_user(),
                db=self.db,
            )
        self.assertFalse(response.success)
        self.assertEqual(response.status, "failed")
        trade = self.db.query(UserTrade).filter(UserTrade.user_id == USER_ID).first()
        self.assertEqual(trade.status, "failed")
        self.assertIsNone(trade.executed_at)


if __name__ == "__main__":
    unittest.main()
