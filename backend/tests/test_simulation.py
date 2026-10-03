"""Tests for the paper-trading fill simulation model.

Covers the two guarantees the paper-trading extension depends on:

* **Bounds** — every simulated slippage stays within the configured
  ``SLIPPAGE_SIM_MIN``..``SLIPPAGE_SIM_MAX`` range (default
  0.3%..3%), including under RNG jitter and at the Polymarket
  price clamps.
* **Monotonicity** — slippage is non-decreasing in order size for
  a fixed book, so larger paper orders never simulate a better
  fill than smaller ones.

Also covers the market-maker variant (band distance from mid),
the midpoint/price fallbacks, and the paper-summary helpers
against a real in-memory SQLite session.
"""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models.base import Base
from app.models.user_settings import UserSettings
from app.models.user_trade import UserTrade
from app.services.simulation import (
    average_entry_price,
    get_paper_summary,
    simulate_fill,
    simulate_market_maker_fill,
)


def _configured_bounds() -> tuple[float, float]:
    settings = get_settings()
    return float(settings.slippage_sim_min), float(settings.slippage_sim_max)


def _market(
    best_bid: float = 0.49,
    best_ask: float = 0.51,
    book_depth: float = 1000.0,
    volume_24hr: float = 100_000.0,
) -> dict:
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "book_depth": book_depth,
        "volume_24hr": volume_24hr,
    }


class SimulateFillBoundsTests(unittest.TestCase):
    def test_slippage_within_configured_bounds(self):
        low, high = _configured_bounds()
        for size in (0.01, 1.0, 10.0, 100.0, 1000.0, 10_000.0, 1e9):
            for side in ("BUY", "SELL"):
                with self.subTest(size=size, side=side):
                    result = simulate_fill(_market(), side, size)
                    self.assertGreaterEqual(result["slippage_bps"], low * 10_000)
                    self.assertLessEqual(result["slippage_bps"], high * 10_000)

    def test_rng_jitter_stays_within_bounds(self):
        low, high = _configured_bounds()

        class _FixedRng:
            """Duck-typed rng whose uniform() always returns ``value``."""

            def __init__(self, value: float):
                self._value = value

            def uniform(self, _a: float, _b: float) -> float:
                return self._value

        # -1.0 → fraction * 0.0 (lower clamp), 1.0 → fraction * 2.0
        # (upper clamp), 0.0 → no jitter.
        for value in (-1.0, 0.0, 1.0):
            result = simulate_fill(_market(), "BUY", 100.0, rng=_FixedRng(value))
            self.assertGreaterEqual(result["slippage_bps"], low * 10_000)
            self.assertLessEqual(result["slippage_bps"], high * 10_000)

    def test_unknown_depth_and_volume_stay_in_bounds(self):
        low, high = _configured_bounds()
        for market in (
            {"best_bid": 0.49, "best_ask": 0.51},
            {"best_bid": 0.49, "best_ask": 0.51, "book_depth": None},
            {"best_bid": 0.49, "best_ask": 0.51, "volume_24hr": 0},
        ):
            result = simulate_fill(market, "SELL", 500.0)
            self.assertGreaterEqual(result["slippage_bps"], low * 10_000)
            self.assertLessEqual(result["slippage_bps"], high * 10_000)


class SimulateFillMonotonicityTests(unittest.TestCase):
    def test_slippage_monotonic_in_size(self):
        previous = -1.0
        for size in (1.0, 10.0, 50.0, 100.0, 500.0, 1000.0, 5000.0, 10_000.0):
            result = simulate_fill(_market(book_depth=1000.0), "BUY", size)
            self.assertGreaterEqual(result["slippage_bps"], previous)
            previous = result["slippage_bps"]

    def test_slippage_monotonic_in_size_for_sell_side(self):
        previous = -1.0
        for size in (1.0, 10.0, 100.0, 1000.0, 10_000.0):
            result = simulate_fill(_market(book_depth=500.0), "SELL", size)
            self.assertGreaterEqual(result["slippage_bps"], previous)
            previous = result["slippage_bps"]

    def test_thinner_book_increases_slippage(self):
        thin = simulate_fill(_market(book_depth=100.0), "BUY", 100.0)
        deep = simulate_fill(_market(book_depth=100_000.0), "BUY", 100.0)
        self.assertGreaterEqual(thin["slippage_bps"], deep["slippage_bps"])

    def test_low_volume_increases_slippage(self):
        thin = simulate_fill(_market(volume_24hr=1000.0), "BUY", 100.0)
        deep = simulate_fill(_market(volume_24hr=10_000_000.0), "BUY", 100.0)
        self.assertGreaterEqual(thin["slippage_bps"], deep["slippage_bps"])


class SimulateFillPriceTests(unittest.TestCase):
    def test_buy_anchors_on_best_ask(self):
        result = simulate_fill(_market(best_bid=0.49, best_ask=0.51), "BUY", 10.0)
        self.assertGreaterEqual(result["fill_price"], 0.51)

    def test_sell_anchors_on_best_bid(self):
        result = simulate_fill(_market(best_bid=0.49, best_ask=0.51), "SELL", 10.0)
        self.assertLessEqual(result["fill_price"], 0.49)

    def test_midpoint_fallback(self):
        result = simulate_fill({"midpoint": 0.5}, "BUY", 10.0)
        self.assertGreaterEqual(result["fill_price"], 0.5)

    def test_price_fallback(self):
        result = simulate_fill({"price": 0.25}, "SELL", 10.0)
        self.assertLessEqual(result["fill_price"], 0.25)

    def test_missing_price_source_raises(self):
        with self.assertRaises(ValueError):
            simulate_fill({}, "BUY", 10.0)

    def test_fill_price_clamped_to_polymarket_range(self):
        high = simulate_fill(_market(best_bid=0.98, best_ask=0.99), "BUY", 1e9)
        self.assertLessEqual(high["fill_price"], 0.9999)
        low = simulate_fill(_market(best_bid=0.01, best_ask=0.02), "SELL", 1e9)
        self.assertGreaterEqual(low["fill_price"], 0.0001)


class SimulateMarketMakerFillTests(unittest.TestCase):
    def test_slippage_within_configured_bounds(self):
        low, high = _configured_bounds()
        for size in (0.01, 10.0, 1000.0, 1e9):
            for side in ("BUY", "SELL"):
                with self.subTest(size=size, side=side):
                    result = simulate_market_maker_fill(
                        _market(), side, size, order_price=0.45, midpoint=0.5
                    )
                    self.assertGreaterEqual(result["slippage_bps"], low * 10_000)
                    self.assertLessEqual(result["slippage_bps"], high * 10_000)

    def test_slippage_monotonic_in_size(self):
        previous = -1.0
        for size in (1.0, 10.0, 100.0, 1000.0, 10_000.0):
            result = simulate_market_maker_fill(
                _market(book_depth=1000.0), "BUY", size, order_price=0.45, midpoint=0.5
            )
            self.assertGreaterEqual(result["slippage_bps"], previous)
            previous = result["slippage_bps"]

    def test_further_band_from_mid_increases_slippage(self):
        near = simulate_market_maker_fill(_market(), "BUY", 10.0, order_price=0.49, midpoint=0.5)
        far = simulate_market_maker_fill(_market(), "BUY", 10.0, order_price=0.40, midpoint=0.5)
        self.assertGreater(far["band_distance"], near["band_distance"])
        self.assertGreaterEqual(far["slippage_bps"], near["slippage_bps"])

    def test_buy_fill_above_band_and_sell_below(self):
        buy = simulate_market_maker_fill(_market(), "BUY", 10.0, order_price=0.45, midpoint=0.5)
        sell = simulate_market_maker_fill(_market(), "SELL", 10.0, order_price=0.55, midpoint=0.5)
        self.assertGreaterEqual(buy["fill_price"], 0.45)
        self.assertLessEqual(sell["fill_price"], 0.55)

    def test_midpoint_defaults_to_book_mid(self):
        explicit = simulate_market_maker_fill(
            _market(best_bid=0.4, best_ask=0.6), "BUY", 10.0, order_price=0.45
        )
        self.assertAlmostEqual(explicit["band_distance"], abs(0.45 - 0.5) / 0.5)


class PaperSummaryTests(unittest.TestCase):
    """Paper summary + average entry price against in-memory SQLite."""

    def setUp(self):
        self.engine = create_engine("sqlite://", future=True)
        Base.metadata.create_all(self.engine, tables=[UserSettings.__table__, UserTrade.__table__])
        self.db = sessionmaker(bind=self.engine, future=True)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _settings(self, **overrides) -> UserSettings:
        values = {
            "user_id": 1,
            "ai_backend": "llm_chain",
            "simulation_mode": True,
            "paper_balance": 1000.0,
        }
        values.update(overrides)
        settings = UserSettings(**values)
        self.db.add(settings)
        self.db.commit()
        self.db.refresh(settings)
        return settings

    def _trade(self, action, price, amount, status="simulated", pnl=None):
        self.db.add(
            UserTrade(
                user_id=1,
                market_id="mkt-a",
                token_id="tok-a",
                action=action,
                amount=amount,
                price=price,
                status=status,
                pnl=pnl,
            )
        )
        self.db.commit()

    def test_summary_defaults_without_settings(self):
        summary = get_paper_summary(self.db, 1)
        self.assertFalse(summary["simulation_mode"])
        self.assertEqual(summary["paper_balance"], 1000.0)
        self.assertEqual(summary["paper_pnl"], 0.0)
        self.assertEqual(summary["simulated_trades"], 0)

    def test_summary_reports_balance_mode_and_pnl(self):
        self._settings(paper_balance=2500.0)
        self._trade("buy", 0.5, 100.0)
        self._trade("sell", 0.7, 100.0, pnl=20.0)
        self._trade("sell", 0.6, 50.0, status="executed", pnl=5.0)

        summary = get_paper_summary(self.db, 1)

        self.assertTrue(summary["simulation_mode"])
        self.assertEqual(summary["paper_balance"], 2500.0)
        # Only the simulated trade's PnL counts.
        self.assertEqual(summary["paper_pnl"], 20.0)
        self.assertEqual(summary["simulated_trades"], 2)

    def test_average_entry_price_is_volume_weighted(self):
        self._trade("buy", 0.5, 100.0)  # 200 shares
        self._trade("buy", 0.6, 60.0)  # 100 shares
        # VWAP = 160 / 300 = 0.5333...
        self.assertAlmostEqual(average_entry_price(self.db, 1, "tok-a"), 160.0 / 300.0, places=4)

    def test_average_entry_price_ignores_sells_and_other_tokens(self):
        self._trade("buy", 0.5, 100.0)
        self._trade("sell", 0.7, 100.0)
        self.db.add(
            UserTrade(
                user_id=1,
                market_id="mkt-b",
                token_id="tok-b",
                action="buy",
                amount=100.0,
                price=0.9,
                status="executed",
            )
        )
        self.db.commit()
        self.assertAlmostEqual(average_entry_price(self.db, 1, "tok-a"), 0.5)

    def test_average_entry_price_none_without_entries(self):
        self.assertIsNone(average_entry_price(self.db, 1, "tok-a"))


if __name__ == "__main__":
    unittest.main()
