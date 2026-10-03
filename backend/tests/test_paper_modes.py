"""Paper-mode tests: with the owning user's ``simulation_mode`` set,
the market maker, inverse bot and stop-loss/take-profit paths make
ZERO CLOB client calls.

Each service is driven through its real decision logic with every
I/O boundary replaced by a local double:

* **Market maker** — ``_sync_market_maker`` computes bands exactly as
  in live mode, then simulates fills against the fetched book depth
  and records ``UserTrade`` rows with ``status="simulated"``. The
  ClobClient constructor, the open-order fetch, the cancel helpers
  and the order-placement helper are all asserted to be untouched.
* **Inverse bot** — ``evaluate_inverse_position_row`` runs the full
  guard ladder (delta trigger, AI recommendation, confidence,
  persistence, cooldown, daily cap) unchanged; the reversal is
  recorded as an ``InverseBotAction`` with ``status="simulated"``
  and no order is submitted.
* **Stop-loss / take-profit** — ``_check_user_price_orders`` still
  evaluates triggers against real (public-book) prices, but the
  close is recorded with a simulated fill price and a simulated
  ``UserTrade`` instead of a sell. A live-mode control test proves
  the mocks would catch a real submission.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.inverse_bot_monitor as inverse_bot_monitor
import app.services.market_maker_service as market_maker_service
import app.services.stop_loss_monitor as stop_loss_monitor
from app.models.inverse_bot_action import InverseBotAction
from app.models.stop_loss import StopLossOrder
from app.models.take_profit import TakeProfitOrder
from app.models.user_trade import UserTrade

# ─────────────── shared doubles ───────────────


def _maker_config(**overrides) -> SimpleNamespace:
    """A MarketMakerConfig stand-in with every field the calculators read."""
    base = {
        "id": 1,
        "user_id": 7,
        "condition_id": "cond-1",
        "token_id_yes": "token-yes",
        "token_id_no": "token-no",
        "strategy": "bands",
        "enabled": True,
        "num_bands": 2,
        "min_spread": 0.02,
        "max_spread": 0.10,
        "band_order_size": 10.0,
        "amm_liquidity": 1000.0,
        "max_collateral": 1000.0,
        "sync_interval_seconds": 30,
        "min_order_size": 0.1,
        "min_price": 0.01,
        "max_price": 0.99,
        "status": "idle",
        "last_sync_at": None,
        "last_error": None,
        "total_orders_placed": 0,
        "total_orders_cancelled": 0,
        "total_volume_usdc": 0.0,
        "current_open_orders": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(user_id: int = 7, wallet: str = "0xwallet") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, wallet_address=wallet)


def _settings(simulation_mode: bool = True, **overrides) -> SimpleNamespace:
    base = {
        "user_id": 7,
        "simulation_mode": simulation_mode,
        "paper_balance": 1000.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _session(first_results=(), all_results=()) -> MagicMock:
    """A session double whose ``.first()``/``.all()`` results are scripted."""
    db = MagicMock(name="db")
    query = MagicMock(name="query")
    db.query.return_value = query
    if first_results:
        query.filter.return_value.first.side_effect = list(first_results)
    else:
        query.filter.return_value.first.return_value = None
    query.filter.return_value.all.return_value = list(all_results)
    return db


def _added_objects(db: MagicMock, model) -> list:
    """Objects of ``model`` passed to ``db.add``."""
    return [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], model)
    ]


BOOK = {
    "best_bid": 0.49,
    "best_ask": 0.51,
    "midpoint": 0.5,
    "bid_depth": 500.0,
    "ask_depth": 500.0,
    "depth": 500.0,
}


# ─────────────── market maker ───────────────


class MarketMakerPaperModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_paper_mode_makes_zero_clob_calls(self):
        config = _maker_config()
        user = _user()
        settings = _settings(simulation_mode=True)
        db = _session(first_results=[config, user, settings])

        fetch_book = AsyncMock(return_value=BOOK)
        fetch_midpoint = AsyncMock(return_value=0.5)
        cancel_all = AsyncMock(return_value=0)
        place_order = AsyncMock(return_value="order-1")
        get_open_orders = AsyncMock(return_value=[])
        cancel_order = AsyncMock(return_value=True)
        clob_cls = MagicMock(name="ClobClient")
        load_creds = MagicMock(return_value={"private_key": "0xpk"})

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_fetch_order_book", fetch_book),
            patch.object(market_maker_service, "_fetch_midpoint", fetch_midpoint),
            patch.object(market_maker_service, "_cancel_all_orders", cancel_all),
            patch.object(market_maker_service, "_place_maker_order", place_order),
            patch.object(market_maker_service, "_get_open_orders", get_open_orders),
            patch.object(market_maker_service, "_cancel_order", cancel_order),
            patch.object(market_maker_service, "load_wallet_credentials", load_creds),
            patch("py_clob_client.client.ClobClient", clob_cls),
        ):
            result = await market_maker_service._sync_market_maker(1)

        # Zero CLOB client calls: no client constructed, no orders
        # fetched, cancelled or placed, no credentials required.
        clob_cls.assert_not_called()
        get_open_orders.assert_not_awaited()
        cancel_order.assert_not_awaited()
        cancel_all.assert_not_awaited()
        place_order.assert_not_awaited()
        load_creds.assert_not_called()
        # Paper mode reads the book (for depth), not the midpoint helper.
        fetch_midpoint.assert_not_awaited()
        fetch_book.assert_awaited_once_with("token-yes")

        # Bands were still computed and every expected order was
        # recorded as a simulated fill.
        self.assertTrue(result["simulated"])
        self.assertEqual(result["cancelled"], 0)
        self.assertEqual(result["placed"], result["expected_orders"])
        self.assertGreater(result["expected_orders"], 0)

        trades = _added_objects(db, UserTrade)
        self.assertEqual(len(trades), result["expected_orders"])
        for trade in trades:
            self.assertEqual(trade.status, "simulated")
            self.assertEqual(trade.user_id, 7)
            self.assertEqual(trade.market_id, "cond-1")
            self.assertIn(trade.action, ("buy", "sell"))

        # Config counters advanced as in live mode.
        self.assertEqual(config.total_orders_placed, result["expected_orders"])
        self.assertEqual(config.current_open_orders, result["expected_orders"])
        self.assertEqual(config.status, "running")
        self.assertIsNone(config.last_error)
        db.commit.assert_called_once()

    async def test_live_mode_still_places_orders(self):
        """Control: with simulation_mode off the CLOB placement path runs."""
        config = _maker_config()
        user = _user()
        settings = _settings(simulation_mode=False)
        db = _session(first_results=[config, user, settings])

        fetch_book = AsyncMock(return_value=BOOK)
        fetch_midpoint = AsyncMock(return_value=0.5)
        cancel_all = AsyncMock(return_value=0)
        place_order = AsyncMock(return_value="order-1")
        load_creds = MagicMock(return_value={"private_key": "0xpk"})

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "_fetch_order_book", fetch_book),
            patch.object(market_maker_service, "_fetch_midpoint", fetch_midpoint),
            patch.object(market_maker_service, "_cancel_all_orders", cancel_all),
            patch.object(market_maker_service, "_place_maker_order", place_order),
            patch.object(market_maker_service, "load_wallet_credentials", load_creds),
        ):
            result = await market_maker_service._sync_market_maker(1)

        self.assertNotIn("simulated", result)
        fetch_midpoint.assert_awaited_once_with("token-yes")
        fetch_book.assert_not_awaited()
        load_creds.assert_called_once_with("0xwallet")
        cancel_all.assert_awaited_once_with("0xpk", "token-yes")
        self.assertEqual(place_order.await_count, result["expected_orders"])
        self.assertEqual(_added_objects(db, UserTrade), [])


# ─────────────── inverse bot ───────────────


def _inverse_row(**overrides) -> SimpleNamespace:
    base = {
        "id": 11,
        "user_id": 3,
        "token_id": "tok-held",
        "condition_id": "cond-1",
        "market_title": "Will it rain?",
        "outcome": "Yes",
        "enabled": True,
        "size_mode_override": "inherit",
        "fixed_amount_override": None,
        "status": "active",
        "last_signal": None,
        "last_confidence": None,
        "last_reasoning": None,
        "last_evaluated_at": None,
        "last_reversed_at": None,
        "reversals_today": 0,
        "reversals_day": None,
        "persistence_count": 0,
        "last_error": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _inverse_settings(**overrides) -> SimpleNamespace:
    base = {
        "user_id": 3,
        "inverse_bot_enabled": True,
        "simulation_mode": True,
        "inverse_bot_default_size_mode": "full_notional",
        "inverse_bot_fixed_amount": 50.0,
        "inverse_bot_confidence_threshold": 75,
        "inverse_bot_cooldown_minutes": 30,
        "inverse_bot_max_reversals_per_day": 3,
        "ai_backend": "llm_chain",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class InverseBotPaperModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_paper_mode_makes_zero_clob_calls(self):
        user = _user(user_id=3)
        settings = _inverse_settings(simulation_mode=True)
        row = _inverse_row()
        db = _session(first_results=[settings])

        positions_by_token = {
            "tok-held": {"asset_id": "tok-held", "size": 100.0, "title": "Will it rain?"}
        }
        market_tokens = AsyncMock(
            return_value={
                "tokens": [
                    {"token_id": "tok-held", "outcome": "Yes", "price": 0.40},
                    {"token_id": "tok-alt", "outcome": "No", "price": 0.60},
                ],
                "question": "Will it rain?",
            }
        )
        ai_eval = AsyncMock(
            return_value={
                "recommendation": "reverse",
                "confidence": 90.0,
                "reasoning": "test reasoning",
                "alt_outcome": "No",
                "alt_token_id": "tok-alt",
            }
        )
        place_order = MagicMock(return_value={"success": True, "order_hash": "0xhash"})
        clob_cls = MagicMock(name="ClobClient")

        with (
            patch.object(inverse_bot_monitor, "_fetch_market_tokens", market_tokens),
            patch.object(inverse_bot_monitor, "_evaluate_with_ai", ai_eval),
            patch.object(inverse_bot_monitor, "_place_order_on_polymarket", place_order),
            patch("py_clob_client.client.ClobClient", clob_cls),
        ):
            result = await inverse_bot_monitor.evaluate_inverse_position_row(
                db=db,
                user=user,
                settings=settings,
                row=row,
                stored_creds=None,
                positions_by_token=positions_by_token,
                force=True,
            )

        # Zero CLOB client calls: no order submitted on either leg.
        clob_cls.assert_not_called()
        place_order.assert_not_called()

        # The reversal was recorded as simulated and the row
        # transitioned exactly as a live success would.
        self.assertEqual(result["status"], "simulated")
        self.assertTrue(result["simulated"])
        actions = _added_objects(db, InverseBotAction)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].status, "simulated")
        self.assertIsNone(actions[0].sell_order_hash)
        self.assertIsNone(actions[0].buy_order_hash)
        self.assertEqual(actions[0].from_token_id, "tok-held")
        self.assertEqual(actions[0].to_token_id, "tok-alt")
        self.assertAlmostEqual(actions[0].buy_notional, 100.0 * 0.40 * 0.985, places=4)

        self.assertEqual(row.token_id, "tok-alt")
        self.assertEqual(row.outcome, "No")
        self.assertEqual(row.status, "active")
        self.assertEqual(row.reversals_today, 1)
        self.assertEqual(row.persistence_count, 0)
        self.assertIsNotNone(row.last_reversed_at)
        db.commit.assert_called_once()

    async def test_live_mode_submits_orders(self):
        """Control: with simulation_mode off both legs are submitted."""
        user = _user(user_id=3)
        settings = _inverse_settings(simulation_mode=False)
        row = _inverse_row()
        db = _session(first_results=[settings])

        positions_by_token = {
            "tok-held": {"asset_id": "tok-held", "size": 100.0, "title": "Will it rain?"}
        }
        market_tokens = AsyncMock(
            return_value={
                "tokens": [
                    {"token_id": "tok-held", "outcome": "Yes", "price": 0.40},
                    {"token_id": "tok-alt", "outcome": "No", "price": 0.60},
                ],
                "question": "Will it rain?",
            }
        )
        ai_eval = AsyncMock(
            return_value={
                "recommendation": "reverse",
                "confidence": 90.0,
                "reasoning": "test reasoning",
                "alt_outcome": "No",
                "alt_token_id": "tok-alt",
            }
        )
        place_order = MagicMock(return_value={"success": True, "order_hash": "0xhash"})

        with (
            patch.object(inverse_bot_monitor, "_fetch_market_tokens", market_tokens),
            patch.object(inverse_bot_monitor, "_evaluate_with_ai", ai_eval),
            patch.object(inverse_bot_monitor, "_place_order_on_polymarket", place_order),
            patch.object(inverse_bot_monitor, "_best_ask_for_token", return_value=0.6),
        ):
            result = await inverse_bot_monitor.evaluate_inverse_position_row(
                db=db,
                user=user,
                settings=settings,
                row=row,
                stored_creds={"private_key": "0xpk", "clob_creds": None},
                positions_by_token=positions_by_token,
                force=True,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(place_order.call_count, 2)
        self.assertEqual(_added_objects(db, InverseBotAction)[0].status, "success")


# ─────────────── stop-loss / take-profit ───────────────


class StopLossPaperModeTests(unittest.IsolatedAsyncioTestCase):
    def _sl_order(self, **overrides) -> StopLossOrder:
        base = {
            "id": 1,
            "user_id": 5,
            "token_id": "tok-sl",
            "market_id": "mkt-sl",
            "market_title": "Will it rain?",
            "outcome": "Yes",
            "size": 100.0,
            "stop_price": 0.50,
            "status": "active",
        }
        base.update(overrides)
        return StopLossOrder(**base)

    def _tp_order(self, **overrides) -> TakeProfitOrder:
        base = {
            "id": 2,
            "user_id": 5,
            "token_id": "tok-tp",
            "market_id": "mkt-tp",
            "market_title": "Will it rain?",
            "outcome": "Yes",
            "size": 100.0,
            "take_profit_price": 0.40,
            "status": "active",
        }
        base.update(overrides)
        return TakeProfitOrder(**base)

    async def test_paper_stop_loss_makes_zero_clob_calls(self):
        settings = _settings(simulation_mode=True, user_id=5)
        db = _session(first_results=[settings])
        sl = self._sl_order()

        fetch_public = AsyncMock(return_value={"tok-sl": 0.30})
        fetch_live = AsyncMock(return_value={"tok-sl": 0.30})
        load_creds = MagicMock(return_value={"private_key": "0xpk", "clob_creds": None})
        place_order = MagicMock(return_value={"success": True, "order_hash": "0xhash"})
        clob_cls = MagicMock(name="ClobClient")

        with (
            patch.object(stop_loss_monitor, "_fetch_public_prices", fetch_public),
            patch.object(stop_loss_monitor, "_fetch_live_prices", fetch_live),
            patch.object(stop_loss_monitor, "load_wallet_credentials", load_creds),
            patch.object(stop_loss_monitor, "_place_order_on_polymarket", place_order),
            patch("py_clob_client.client.ClobClient", clob_cls),
        ):
            triggered = await stop_loss_monitor._check_user_price_orders(
                db=db,
                user_id=5,
                wallet_address="0xwallet",
                sl_orders=[sl],
                tp_orders=[],
            )

        # Zero CLOB client calls: no credentials, no authenticated
        # price fetch, no sell submitted.
        clob_cls.assert_not_called()
        load_creds.assert_not_called()
        fetch_live.assert_not_awaited()
        place_order.assert_not_called()

        # The trigger was still evaluated against a real price and
        # the close was recorded as simulated.
        self.assertEqual(triggered, 1)
        self.assertEqual(sl.status, "triggered")
        self.assertIsNone(sl.order_hash)
        self.assertIsNotNone(sl.executed_price)
        self.assertLessEqual(sl.executed_price, 0.30)
        self.assertIsNotNone(sl.triggered_at)

        trades = _added_objects(db, UserTrade)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].status, "simulated")
        self.assertEqual(trades[0].action, "sell")
        self.assertEqual(trades[0].token_id, "tok-sl")
        self.assertEqual(trades[0].user_id, 5)
        db.commit.assert_called_once()

    async def test_paper_take_profit_makes_zero_clob_calls(self):
        settings = _settings(simulation_mode=True, user_id=5)
        db = _session(first_results=[settings])
        tp = self._tp_order()

        fetch_public = AsyncMock(return_value={"tok-tp": 0.55})
        place_order = MagicMock(return_value={"success": True, "order_hash": "0xhash"})

        with (
            patch.object(stop_loss_monitor, "_fetch_public_prices", fetch_public),
            patch.object(stop_loss_monitor, "_place_order_on_polymarket", place_order),
        ):
            triggered = await stop_loss_monitor._check_user_price_orders(
                db=db,
                user_id=5,
                wallet_address="0xwallet",
                sl_orders=[],
                tp_orders=[tp],
            )

        place_order.assert_not_called()
        self.assertEqual(triggered, 1)
        self.assertEqual(tp.status, "triggered")
        self.assertIsNone(tp.order_hash)
        # A simulated SELL fills at or below the trigger price
        # (depth-aware slippage), never above it.
        self.assertLessEqual(tp.executed_price, 0.55)
        self.assertGreater(tp.executed_price, 0.55 * (1 - 0.03))
        trades = _added_objects(db, UserTrade)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].status, "simulated")

    async def test_paper_mode_computes_pnl_against_average_entry(self):
        settings = _settings(simulation_mode=True, user_id=5)
        db = _session(first_results=[settings], all_results=[])
        sl = self._sl_order()

        # Average entry: 100 USDC @ 0.40 + 100 USDC @ 0.50
        # → 250 + 200 = 450 shares for 200 USDC → VWAP = 0.4444
        entry_rows = [
            UserTrade(
                user_id=5,
                market_id="mkt-sl",
                token_id="tok-sl",
                action="buy",
                amount=100.0,
                price=0.40,
                status="executed",
            ),
            UserTrade(
                user_id=5,
                market_id="mkt-sl",
                token_id="tok-sl",
                action="buy",
                amount=100.0,
                price=0.50,
                status="executed",
            ),
        ]
        db.query.return_value.filter.return_value.all.return_value = entry_rows

        fetch_public = AsyncMock(return_value={"tok-sl": 0.30})

        with (
            patch.object(stop_loss_monitor, "_fetch_public_prices", fetch_public),
            patch.object(stop_loss_monitor, "_place_order_on_polymarket", MagicMock()),
        ):
            await stop_loss_monitor._check_user_price_orders(
                db=db,
                user_id=5,
                wallet_address="0xwallet",
                sl_orders=[sl],
                tp_orders=[],
            )

        trades = _added_objects(db, UserTrade)
        self.assertEqual(len(trades), 1)
        # The simulated SELL fills below the 0.30 trigger.
        self.assertLess(trades[0].price, 0.30)
        # size is a share count, so realized PnL is
        # size * (fill - average entry).
        entry = 200.0 / 450.0
        expected_pnl = round(100.0 * (trades[0].price - entry), 4)
        self.assertIsNotNone(trades[0].pnl)
        self.assertAlmostEqual(trades[0].pnl, expected_pnl, places=4)

    async def test_live_mode_submits_the_sell(self):
        """Control: with simulation_mode off the sell is submitted."""
        settings = _settings(simulation_mode=False, user_id=5)
        db = _session(first_results=[settings])
        sl = self._sl_order()

        fetch_live = AsyncMock(return_value={"tok-sl": 0.30})
        load_creds = MagicMock(return_value={"private_key": "0xpk", "clob_creds": None})
        place_order = MagicMock(return_value={"success": True, "order_hash": "0xabc"})

        with (
            patch.object(stop_loss_monitor, "_fetch_live_prices", fetch_live),
            patch.object(stop_loss_monitor, "load_wallet_credentials", load_creds),
            patch.object(stop_loss_monitor, "_place_order_on_polymarket", place_order),
        ):
            triggered = await stop_loss_monitor._check_user_price_orders(
                db=db,
                user_id=5,
                wallet_address="0xwallet",
                sl_orders=[sl],
                tp_orders=[],
            )

        self.assertEqual(triggered, 1)
        load_creds.assert_called_once_with("0xwallet")
        fetch_live.assert_awaited_once()
        place_order.assert_called_once()
        self.assertEqual(sl.status, "triggered")
        self.assertEqual(sl.order_hash, "0xabc")
        self.assertEqual(sl.executed_price, 0.30)
        self.assertEqual(_added_objects(db, UserTrade), [])


if __name__ == "__main__":
    unittest.main()
