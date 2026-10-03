import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.stop_loss_monitor as stop_loss_monitor
from app.models.stop_loss import StopLossOrder
from app.models.take_profit import TakeProfitOrder


class StopLossMonitorPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_stop_losses_batches_user_wallet_lookup(self):
        """The monitor must resolve each wallet once, not once per order.

        Resolving credentials is the expensive part of the cycle, so
        ``_check_stop_losses`` eager-loads the user relationship and dispatches
        per user, rather than querying the user table once per order.
        """

        def _sl_order(order_id, user_id, token_id, stop_price, wallet):
            return SimpleNamespace(
                id=order_id,
                user_id=user_id,
                token_id=token_id,
                stop_price=stop_price,
                size=1.0,
                # Populated by the eager-loaded relationship.
                user=SimpleNamespace(wallet_address=wallet),
            )

        sl_orders = [
            _sl_order(1, 1, "tok-1", 0.4, "0x111"),
            _sl_order(2, 1, "tok-2", 0.6, "0x111"),
            _sl_order(3, 2, "tok-3", 0.7, "0x222"),
        ]
        tp_orders = []

        db = MagicMock()

        sl_query = MagicMock()
        sl_query.options.return_value = sl_query
        sl_query.filter.return_value = sl_query
        sl_query.all.return_value = sl_orders

        tp_query = MagicMock()
        tp_query.options.return_value = tp_query
        tp_query.filter.return_value = tp_query
        tp_query.all.return_value = tp_orders

        # Two queries total: stop-loss orders and take-profit orders. No
        # per-order or per-user query is permitted.
        db.query.side_effect = [sl_query, tp_query]

        with (
            patch.object(stop_loss_monitor, "SessionLocal", return_value=db),
            patch.object(
                stop_loss_monitor,
                "_check_user_price_orders",
                new=AsyncMock(return_value=0),
            ) as check_user_mock,
        ):
            await stop_loss_monitor._check_stop_losses()

        self.assertEqual(db.query.call_count, 2)
        db.query.assert_has_calls([call_for(StopLossOrder), call_for(TakeProfitOrder)])

        # One dispatch per distinct user, not per order.
        self.assertEqual(check_user_mock.await_count, 2)
        called_wallets = {c.kwargs["wallet_address"] for c in check_user_mock.await_args_list}
        self.assertEqual(called_wallets, {"0x111", "0x222"})

        # The user holding two orders must receive both in a single call.
        by_wallet = {
            c.kwargs["wallet_address"]: c.kwargs["sl_orders"]
            for c in check_user_mock.await_args_list
        }
        self.assertEqual(len(by_wallet["0x111"]), 2)
        self.assertEqual(len(by_wallet["0x222"]), 1)


def call_for(entity):
    """`unittest.mock.call(entity)`, imported lazily to keep the header short."""
    from unittest.mock import call

    return call(entity)


if __name__ == "__main__":
    unittest.main()
