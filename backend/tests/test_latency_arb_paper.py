"""Paper-mode end-to-end tests for the latency engine (plan 06).

Drives the real engine cycle (``_engine_cycle``) with every
I/O boundary replaced by a local double — mirroring plan 08's
``test_paper_modes.py`` patterns:

* **Paper mode** — with the owning user's ``simulation_mode``
  set, the engine evaluates the full edge model, records a
  ``UserTrade`` with ``status="simulated"`` and
  ``strategy_source="latency_arb"`` (expected price/size
  populated for plan 03 analytics), and makes ZERO CLOB
  calls: the ClobClient constructor, the public book fetch
  and the FOK placement helper are all asserted untouched.
* **Live control** — with ``simulation_mode`` off and
  ``LATENCY_ARB_LIVE`` on, the book fetch and FOK placement
  run (proving the mocks would catch a real submission) and
  the trade is recorded ``pending`` with its order hash.
* **Latency budget** — a stale feed (feed→order over the
  1500ms budget) aborts before any order path.
* **Risk gates** — the per-strategy daily loss limit and the
  consecutive-loss circuit breaker reject before execution.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.latency_arb_service as latency_arb_service
from app.config import Settings
from app.models.user_trade import UserTrade
from app.services.crypto_markets_service import CryptoMarket


def _calm_closes(n: int = 60) -> list[float]:
    """Synthetic 1m closes alternating ±20 around 67000."""
    return [67000.0 + (20.0 if i % 2 else -20.0) for i in range(n)]


def _market() -> CryptoMarket:
    """A synthetic BTC 5m up/down market for the current window."""
    now = datetime.now(UTC)
    window_start = int(now.timestamp()) - (int(now.timestamp()) % 300)
    return CryptoMarket(
        symbol="BTC",
        window_minutes=5,
        window_start_epoch=window_start,
        condition_id="cond-btc-5m",
        token_ids={"up": "token-up-1", "down": "token-down-1"},
        prices={"up": 0.5, "down": 0.5},
        question="Bitcoin up or down in 5 minutes?",
    )


def _settings(simulation_mode: bool = True) -> SimpleNamespace:
    """A UserSettings stand-in with every field the gates read."""
    return SimpleNamespace(
        user_id=7,
        simulation_mode=simulation_mode,
        paper_balance=1000.0,
        trading_halted=False,
        halt_reason=None,
        cooldown_until=None,
        monthly_loss_limit=None,
        max_drawdown_pct=25.0,
        peak_capital=None,
        total_loss_halt_pct=40.0,
        initial_capital=None,
        max_position_size=100.0,
        daily_loss_limit=500.0,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=7, wallet_address="0xwallet")


def _config(**overrides) -> SimpleNamespace:
    """A LatencyArbConfig stand-in."""
    base = {
        "user_id": 7,
        "enabled": True,
        "edge_threshold": 0.03,
        "max_notional": 50.0,
        "symbols": ["BTC"],
        "windows": [5],
        "late_entry": False,
        "daily_loss_limit": 20.0,
        "alert_on_opportunity": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _session() -> MagicMock:
    """A session double whose query chains return safe defaults."""
    db = MagicMock(name="db")
    query = MagicMock(name="query")
    db.query.return_value = query
    # Aggregate queries (.scalar()).
    query.filter.return_value.scalar.return_value = 0.0
    # Row queries (.first()).
    query.filter.return_value.first.return_value = None
    # List queries (.all()).
    query.filter.return_value.all.return_value = []
    # Ordered list queries (circuit breaker).
    query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    return db


def _added_objects(db: MagicMock, model) -> list:
    """Objects of ``model`` passed to ``db.add``."""
    return [
        call.args[0]
        for call in db.add.call_args_list
        if call.args and isinstance(call.args[0], model)
    ]


def _engine_settings(**overrides) -> Settings:
    """Real Settings with engine overrides (deterministic
    late-entry so the test never depends on wall-clock
    position within the window)."""
    kwargs = {
        "latency_arb_late_entry": True,
        "latency_arb_live": False,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


@contextmanager
def _engine_patches(
    db,
    config,
    settings,
    user,
    market,
    *,
    engine_settings=None,
    feed_lag_ms=50.0,
    last_price=67234.5,
    fetch_book=None,
    place_fok_order=None,
    dispatch_alert=None,
    clob_cls=None,
    extra=(),
):
    """Patch every I/O boundary ``_engine_cycle`` touches.

    The mocks the caller wants to assert on are passed in and
    wired into the patch stack; everything else gets a safe
    default double. ``extra`` holds additional patches (the
    live-mode credential and proxy-wallet patches).
    """
    if engine_settings is None:
        engine_settings = _engine_settings()
    if fetch_book is None:
        fetch_book = AsyncMock(name="fetch_book")
    if place_fok_order is None:
        place_fok_order = AsyncMock(name="place_fok_order")
    if dispatch_alert is None:
        dispatch_alert = MagicMock(name="dispatch_alert")
    if clob_cls is None:
        clob_cls = MagicMock(name="ClobClient")

    def _get_market(symbol, window_minutes, window_start_epoch=None):
        if (symbol, window_minutes) == ("BTC", 5):
            return market
        return None

    patches = [
        patch.object(latency_arb_service, "get_settings", lambda: engine_settings),
        patch.object(latency_arb_service, "SessionLocal", return_value=db),
        patch.object(
            latency_arb_service,
            "_enabled_configs",
            return_value=[(config, settings, user)],
        ),
        patch.object(
            latency_arb_service,
            "_dispatch_opportunity_alert",
            dispatch_alert,
        ),
        patch.object(
            latency_arb_service.crypto_markets_service,
            "markets_stale",
            return_value=False,
        ),
        patch.object(
            latency_arb_service.crypto_markets_service,
            "get_market",
            side_effect=_get_market,
        ),
        patch.object(
            latency_arb_service.crypto_markets_service,
            "window_start_matches",
            return_value=True,
        ),
        patch.object(
            latency_arb_service.binance_ws_service,
            "get_last_price",
            side_effect=lambda symbol: last_price if symbol == "BTCUSDT" else None,
        ),
        patch.object(
            latency_arb_service.binance_ws_service,
            "get_window_open_price",
            return_value=67000.0,
        ),
        patch.object(
            latency_arb_service.binance_ws_service,
            "get_recent_closes",
            return_value=_calm_closes(),
        ),
        patch.object(
            latency_arb_service.binance_ws_service,
            "get_feed_lag_ms",
            return_value=feed_lag_ms,
        ),
        patch.object(latency_arb_service, "_fetch_book", fetch_book),
        patch.object(latency_arb_service, "_place_fok_order", place_fok_order),
        patch("py_clob_client.client.ClobClient", clob_cls),
    ]
    patches.extend(extra)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        yield


class LatencyArbPaperModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_paper_mode_makes_zero_clob_calls(self):
        db = _session()
        market = _market()
        settings = _settings(simulation_mode=True)
        config = _config()
        user = _user()

        clob_cls = MagicMock(name="ClobClient")
        fetch_book = AsyncMock(name="fetch_book")
        place_fok_order = AsyncMock(name="place_fok_order")
        dispatch_alert = MagicMock(name="dispatch_alert")

        with _engine_patches(
            db,
            config,
            settings,
            user,
            market,
            fetch_book=fetch_book,
            place_fok_order=place_fok_order,
            dispatch_alert=dispatch_alert,
            clob_cls=clob_cls,
        ):
            await latency_arb_service._engine_cycle()

        # Zero CLOB calls: no client constructed, no book
        # fetched, no order placed.
        clob_cls.assert_not_called()
        fetch_book.assert_not_awaited()
        place_fok_order.assert_not_awaited()

        # The edge was detected and a simulated fill recorded.
        trades = _added_objects(db, UserTrade)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade.status, "simulated")
        self.assertEqual(trade.strategy_source, "latency_arb")
        self.assertEqual(trade.user_id, 7)
        self.assertEqual(trade.market_id, "cond-btc-5m")
        self.assertEqual(trade.token_id, "token-up-1")
        self.assertEqual(trade.action, "buy")
        # Expected price/size recorded for plan 03 analytics.
        self.assertEqual(trade.expected_price, 0.5)
        self.assertEqual(trade.expected_size, 50.0)
        self.assertEqual(trade.amount, 50.0)
        # The simulated fill sits within the plan 08 slippage
        # bounds (0.3%–3% away from the 0.50 reference).
        self.assertGreaterEqual(trade.price, 0.5015)
        self.assertLessEqual(trade.price, 0.515)
        self.assertIsNotNone(trade.filled_price)
        self.assertIsNotNone(trade.slippage_bps)
        self.assertGreaterEqual(trade.slippage_bps, 30.0)
        self.assertLessEqual(trade.slippage_bps, 300.0)
        self.assertIsNotNone(trade.latency_ms)
        self.assertIsNotNone(trade.executed_at)
        self.assertIn("p_model", trade.calculation_details)
        db.commit.assert_called_once()
        # The opportunity alert fired for the paper fill.
        dispatch_alert.assert_called_once()

    async def test_live_mode_places_fok_order(self):
        """Control: with simulation_mode off and
        LATENCY_ARB_LIVE on, the book fetch and FOK
        placement run and the trade is recorded pending."""
        db = _session()
        market = _market()
        settings = _settings(simulation_mode=False)
        config = _config()
        user = _user()

        clob_cls = MagicMock(name="ClobClient")
        book = {
            "bids": [[0.499, 1000.0]],
            "asks": [[0.501, 1000.0]],
        }
        fetch_book = AsyncMock(return_value=book)
        place_fok_order = AsyncMock(return_value={"success": True, "orderID": "0xabc123"})
        load_creds = MagicMock(
            return_value={
                "private_key": "0x" + "11" * 32,
                "clob_creds": None,
            }
        )

        with _engine_patches(
            db,
            config,
            settings,
            user,
            market,
            engine_settings=_engine_settings(latency_arb_live=True),
            fetch_book=fetch_book,
            place_fok_order=place_fok_order,
            clob_cls=clob_cls,
            extra=[
                patch.object(
                    latency_arb_service,
                    "_get_poly_proxy_wallet_address",
                    return_value="0xproxy",
                ),
                patch(
                    "app.security.credential_store.load_wallet_credentials",
                    load_creds,
                ),
            ],
        ):
            await latency_arb_service._engine_cycle()

        # The live path ran: book fetched, FOK placed with
        # the underpriced up token at the 0.50 reference.
        fetch_book.assert_awaited_once_with("token-up-1")
        place_fok_order.assert_awaited_once()
        order_args = place_fok_order.await_args.args
        self.assertEqual(order_args[3], "token-up-1")
        self.assertAlmostEqual(order_args[4], 0.5)
        self.assertAlmostEqual(order_args[5], 50.0)
        # No real ClobClient was constructed (the placement
        # helper is the I/O boundary under test).
        clob_cls.assert_not_called()

        trades = _added_objects(db, UserTrade)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade.status, "pending")
        self.assertEqual(trade.strategy_source, "latency_arb")
        self.assertEqual(trade.order_hash, "0xabc123")
        self.assertEqual(trade.expected_price, 0.5)
        self.assertEqual(trade.expected_size, 50.0)
        self.assertIsNone(trade.executed_at)
        db.commit.assert_called_once()

    async def test_stale_feed_aborts_before_order(self):
        """A feed→order latency over the budget aborts with
        no trade and no CLOB calls."""
        db = _session()
        market = _market()
        settings = _settings(simulation_mode=True)
        config = _config()
        user = _user()

        clob_cls = MagicMock(name="ClobClient")
        fetch_book = AsyncMock(name="fetch_book")
        place_fok_order = AsyncMock(name="place_fok_order")

        # 2000ms feed lag exceeds the 1500ms budget.
        with _engine_patches(
            db,
            config,
            settings,
            user,
            market,
            feed_lag_ms=2000.0,
            fetch_book=fetch_book,
            place_fok_order=place_fok_order,
            clob_cls=clob_cls,
        ):
            await latency_arb_service._engine_cycle()

        clob_cls.assert_not_called()
        fetch_book.assert_not_awaited()
        place_fok_order.assert_not_awaited()
        self.assertEqual(_added_objects(db, UserTrade), [])
        db.commit.assert_not_called()

    async def test_strategy_daily_loss_limit_rejects(self):
        """The per-strategy daily loss limit rejects before
        execution."""
        db = _session()
        market = _market()
        settings = _settings(simulation_mode=True)
        config = _config(daily_loss_limit=20.0)
        user = _user()

        # All aggregate scalar queries return 25.0: the
        # strategy's daily loss (25.0) exceeds the 20.0
        # limit while the shared gates still pass (daily
        # loss limit 500.0, position cap 100.0).
        db.query.return_value.filter.return_value.scalar.return_value = 25.0

        with _engine_patches(db, config, settings, user, market):
            await latency_arb_service._engine_cycle()

        self.assertEqual(_added_objects(db, UserTrade), [])

    async def test_circuit_breaker_rejects_after_losses(self):
        """Three consecutive losses open the circuit breaker
        and reject the trade."""
        db = _session()
        market = _market()
        settings = _settings(simulation_mode=True)
        config = _config()
        user = _user()

        # Three consecutive settled losses, most recent
        # first — the breaker is open for 10 minutes.
        now = datetime.now(UTC)
        loss_rows = [SimpleNamespace(pnl=-10.0, created_at=now, updated_at=now) for _ in range(3)]
        limit_query = db.query.return_value.filter.return_value.order_by.return_value.limit
        ordered_query = limit_query.return_value
        ordered_query.all.return_value = loss_rows

        with _engine_patches(db, config, settings, user, market):
            await latency_arb_service._engine_cycle()

        self.assertEqual(_added_objects(db, UserTrade), [])

    async def test_no_opportunity_when_edge_below_threshold(self):
        """A market that has already adjusted (price ≈
        model) produces no opportunity and no trade."""
        db = _session()
        # Market already prices the move: up at 0.99 while
        # the model (conservative σ) says ≈ 0.5 — but with
        # a tiny fresh distance the implied σ dominates and
        # the edge collapses. Use a near-coin-flip market
        # with a negligible move instead.
        market = _market()
        market.prices = {"up": 0.5001, "down": 0.4999}
        settings = _settings(simulation_mode=True)
        config = _config()
        user = _user()

        # Negligible move since the window opened.
        with _engine_patches(
            db,
            config,
            settings,
            user,
            market,
            last_price=67000.5,
        ):
            await latency_arb_service._engine_cycle()

        self.assertEqual(_added_objects(db, UserTrade), [])


if __name__ == "__main__":
    unittest.main()
