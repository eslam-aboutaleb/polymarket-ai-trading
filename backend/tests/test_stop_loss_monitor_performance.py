import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.models.stop_loss import StopLossOrder
from app.models.user import User
import app.services.stop_loss_monitor as stop_loss_monitor


class StopLossMonitorPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_stop_losses_batches_user_wallet_lookup(self):
        orders = [
            SimpleNamespace(id=1, user_id=1, token_id="tok-1", stop_price=0.4, size=1.0),
            SimpleNamespace(id=2, user_id=2, token_id="tok-2", stop_price=0.6, size=2.0),
        ]

        db = MagicMock()

        orders_query = MagicMock()
        orders_query.filter.return_value = orders_query
        orders_query.all.return_value = orders

        users_query = MagicMock()
        users_query.filter.return_value = users_query
        users_query.all.return_value = [(1, "0x111"), (2, "0x222")]

        db.query.side_effect = [orders_query, users_query]

        with patch.object(stop_loss_monitor, "SessionLocal", return_value=db), patch.object(
            stop_loss_monitor,
            "_check_user_stop_losses",
            new=AsyncMock(return_value=0),
        ) as check_user_mock:
            await stop_loss_monitor._check_stop_losses()

        self.assertEqual(db.query.call_count, 2)
        db.query.assert_has_calls(
            [
                call(StopLossOrder),
                call(User.id, User.wallet_address),
            ]
        )
        self.assertEqual(check_user_mock.await_count, 2)
        called_wallets = {c.kwargs["wallet_address"] for c in check_user_mock.await_args_list}
        self.assertEqual(called_wallets, {"0x111", "0x222"})


if __name__ == "__main__":
    unittest.main()
