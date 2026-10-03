"""Edge-model unit tests for the latency-arbitrage engine (plan 06).

Covers the pure model functions with synthetic price paths:

* normal CDF / inverse-CDF round trips,
* distance, t_remaining and realized-σ computation,
* implied σ (coin-flip market → 0; extreme market → large),
* the conservative σ guard (max of realized and 2× implied),
* Bachelier P(Up): in-the-money → P→1 as t→0,
  out-of-the-money → P→0, at-the-money → 0.5,
* edge computation and full opportunity evaluation
  (candidate only when the gap meets the threshold).
"""

from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta

from app.services.crypto_markets_service import CryptoMarket
from app.services.latency_arb_service import (
    DEFAULT_SIGMA,
    compute_distance,
    compute_edge,
    compute_t_remaining,
    conservative_sigma,
    evaluate_opportunity,
    implied_volatility,
    normal_cdf,
    normal_ppf,
    probability_up,
    realized_volatility,
)


def _market(up_price: float = 0.5) -> CryptoMarket:
    """A synthetic BTC 5m up/down market."""
    now = datetime.now(UTC)
    window_start = int(now.timestamp()) - (int(now.timestamp()) % 300)
    return CryptoMarket(
        symbol="BTC",
        window_minutes=5,
        window_start_epoch=window_start,
        condition_id="cond-test-1",
        token_ids={"up": "token-up-1", "down": "token-down-1"},
        prices={"up": up_price, "down": round(1.0 - up_price, 4)},
        question="Bitcoin up or down in 5 minutes?",
    )


def _calm_closes(n: int = 60) -> list[float]:
    """Synthetic 1m closes alternating ±20 around 67000."""
    return [67000.0 + (20.0 if i % 2 else -20.0) for i in range(n)]


def _trending_closes(n: int = 60) -> list[float]:
    """Synthetic 1m closes in a strong uptrend."""
    return [67000.0 + 10.0 * i for i in range(n)]


class NormalDistributionTests(unittest.TestCase):
    def test_cdf_symmetry(self):
        self.assertAlmostEqual(normal_cdf(0.0), 0.5)
        self.assertAlmostEqual(normal_cdf(1.0), 1.0 - normal_cdf(-1.0))
        self.assertAlmostEqual(normal_cdf(1.959964), 0.975, places=5)

    def test_ppf_cdf_roundtrip(self):
        for p in (0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
            self.assertAlmostEqual(normal_cdf(normal_ppf(p)), p, places=8)

    def test_ppf_bounds(self):
        self.assertEqual(normal_ppf(0.0), float("-inf"))
        self.assertEqual(normal_ppf(1.0), float("inf"))
        self.assertAlmostEqual(normal_ppf(0.5), 0.0)


class DistanceAndTimeTests(unittest.TestCase):
    def test_compute_distance(self):
        self.assertAlmostEqual(compute_distance(110.0, 100.0), 0.1)
        self.assertAlmostEqual(compute_distance(90.0, 100.0), -0.1)
        self.assertEqual(compute_distance(100.0, 100.0), 0.0)
        # Zero window open is guarded.
        self.assertEqual(compute_distance(100.0, 0.0), 0.0)

    def test_compute_t_remaining_clamps(self):
        now = datetime.now(UTC)
        start = now - timedelta(seconds=150)
        # Half of a 5m window elapsed → 0.5 remaining.
        self.assertAlmostEqual(
            compute_t_remaining(start.timestamp(), 5, now),
            0.5,
            places=3,
        )
        # Window not started → clamped to 1.0.
        future = now + timedelta(minutes=5)
        self.assertEqual(
            compute_t_remaining(future.timestamp(), 5, now),
            1.0,
        )
        # Window ended → clamped to 0.0.
        past = now - timedelta(minutes=10)
        self.assertEqual(
            compute_t_remaining(past.timestamp(), 5, now),
            0.0,
        )


class VolatilityTests(unittest.TestCase):
    def test_realized_volatility_scales_with_window(self):
        closes = _calm_closes()
        sigma_5m = realized_volatility(closes, 5)
        sigma_15m = realized_volatility(closes, 15)
        sigma_60m = realized_volatility(closes, 60)
        # Per-window σ grows with √window_minutes.
        self.assertAlmostEqual(sigma_15m / sigma_5m, math.sqrt(15.0 / 5.0), places=6)
        self.assertAlmostEqual(sigma_60m / sigma_5m, math.sqrt(60.0 / 5.0), places=6)
        self.assertGreater(sigma_5m, 0.0)

    def test_realized_volatility_trending_path(self):
        # A smooth trend has near-zero return variance.
        sigma = realized_volatility(_trending_closes(), 5)
        self.assertLess(sigma, realized_volatility(_calm_closes(), 5))

    def test_realized_volatility_fallback(self):
        self.assertEqual(realized_volatility([], 5), DEFAULT_SIGMA)
        self.assertEqual(realized_volatility([100.0], 5), DEFAULT_SIGMA)
        self.assertEqual(realized_volatility([100.0, 100.0, 100.0], 5), 0.0)

    def test_implied_volatility_coin_flip_is_zero(self):
        # A 0.50 market price carries no volatility information.
        self.assertEqual(implied_volatility(0.5, 0.5), 0.0)

    def test_implied_volatility_extreme_is_large(self):
        implied = implied_volatility(0.9, 0.25)
        # z(0.9) ≈ 1.2816 → 1.2816 / √0.25 ≈ 2.563
        self.assertAlmostEqual(implied, 1.2815515655446004 / 0.5, places=6)

    def test_implied_volatility_zero_time(self):
        self.assertEqual(implied_volatility(0.9, 0.0), 0.0)

    def test_conservative_sigma(self):
        # max(realized, 2×implied) — never below realized.
        self.assertEqual(conservative_sigma(0.01, 0.002), 0.01)
        self.assertEqual(conservative_sigma(0.001, 0.004), 0.008)
        self.assertEqual(conservative_sigma(0.0, 0.0), 0.0)


class ProbabilityTests(unittest.TestCase):
    def test_in_the_money_converges_to_one_as_t_goes_to_zero(self):
        # Synthetic path: price moved up 0.5% since the window
        # opened. As the window runs out, P(Up) → 1.
        distance = compute_distance(67335.0, 67000.0)
        self.assertGreater(distance, 0.0)
        sigma = 0.001
        for t_remaining in (1.0, 0.5, 0.1, 0.01, 0.001, 1e-6):
            p = probability_up(distance, sigma, t_remaining)
            self.assertGreater(p, 0.9)
        self.assertAlmostEqual(probability_up(distance, sigma, 1e-9), 1.0, places=6)

    def test_out_of_the_money_converges_to_zero_as_t_goes_to_zero(self):
        distance = compute_distance(66665.0, 67000.0)
        self.assertLess(distance, 0.0)
        sigma = 0.001
        for t_remaining in (1.0, 0.1, 0.01, 1e-6):
            p = probability_up(distance, sigma, t_remaining)
            self.assertLess(p, 0.1)
        self.assertAlmostEqual(probability_up(distance, sigma, 1e-9), 0.0, places=6)

    def test_at_the_money_is_coin_flip(self):
        for t_remaining in (1.0, 0.5, 0.1):
            self.assertAlmostEqual(probability_up(0.0, 0.001, t_remaining), 0.5)

    def test_degenerate_sigma_uses_distance_sign(self):
        self.assertEqual(probability_up(0.01, 0.0, 0.5), 1.0)
        self.assertEqual(probability_up(-0.01, 0.0, 0.5), 0.0)
        self.assertEqual(probability_up(0.0, 0.0, 0.5), 0.5)

    def test_monotonic_in_distance(self):
        sigma = 0.001
        previous = 0.0
        for distance in (-0.01, -0.005, 0.0, 0.005, 0.01):
            p = probability_up(distance, sigma, 0.5)
            self.assertGreaterEqual(p, previous)
            previous = p


class EdgeTests(unittest.TestCase):
    def test_compute_edge(self):
        self.assertAlmostEqual(compute_edge(0.7, 0.5), 0.2)
        self.assertAlmostEqual(compute_edge(0.4, 0.5), 0.1)
        self.assertEqual(compute_edge(0.5, 0.5), 0.0)

    def test_evaluate_opportunity_candidate(self):
        # Fresh Binance move (+0.35%) vs a coin-flip market
        # that has not adjusted: large edge, side = up.
        market = _market(up_price=0.5)
        now = datetime.now(UTC)
        opportunity = evaluate_opportunity(
            "BTC",
            5,
            market,
            current_price=67234.5,
            window_open=67000.0,
            now=now,
            kline_closes=_calm_closes(),
            feed_lag_ms=50.0,
            edge_threshold=0.03,
        )
        self.assertIsNotNone(opportunity)
        assert opportunity is not None
        self.assertEqual(opportunity.side, "up")
        self.assertGreaterEqual(opportunity.edge, 0.03)
        self.assertGreater(opportunity.p_model, opportunity.p_market)
        self.assertAlmostEqual(opportunity.distance, 234.5 / 67000.0, places=6)
        self.assertEqual(opportunity.symbol, "BTC")
        self.assertEqual(opportunity.window_minutes, 5)
        self.assertEqual(opportunity.feed_lag_ms, 50.0)

    def test_evaluate_opportunity_down_side(self):
        # Price fell since the window opened while the market
        # still prices a coin flip → Down is underpriced.
        market = _market(up_price=0.5)
        now = datetime.now(UTC)
        opportunity = evaluate_opportunity(
            "BTC",
            5,
            market,
            current_price=66765.5,
            window_open=67000.0,
            now=now,
            kline_closes=_calm_closes(),
            edge_threshold=0.03,
        )
        self.assertIsNotNone(opportunity)
        assert opportunity is not None
        self.assertEqual(opportunity.side, "down")
        self.assertLess(opportunity.p_model, opportunity.p_market)

    def test_evaluate_opportunity_below_threshold(self):
        # Tiny move vs an already-adjusted market: no edge.
        market = _market(up_price=0.51)
        now = datetime.now(UTC)
        opportunity = evaluate_opportunity(
            "BTC",
            5,
            market,
            current_price=67003.0,
            window_open=67000.0,
            now=now,
            kline_closes=_calm_closes(),
            edge_threshold=0.03,
        )
        self.assertIsNone(opportunity)

    def test_evaluate_opportunity_expired_window(self):
        # A window that has already ended cannot be traded.
        market = _market(up_price=0.5)
        market.window_start_epoch = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())
        opportunity = evaluate_opportunity(
            "BTC",
            5,
            market,
            current_price=67234.5,
            window_open=67000.0,
            now=datetime.now(UTC),
            kline_closes=_calm_closes(),
            edge_threshold=0.03,
        )
        self.assertIsNone(opportunity)

    def test_evaluate_opportunity_conservative_sigma_bounds_model(
        self,
    ):
        # An extreme market price (0.9) implies a huge σ; the
        # conservative guard inflates σ to 2× implied, pulling
        # P_model back toward 0.5 and bounding the model's
        # confidence — the model-risk note in the plan.
        market = _market(up_price=0.9)
        now = datetime.now(UTC)
        opportunity = evaluate_opportunity(
            "BTC",
            5,
            market,
            current_price=67000.0,
            window_open=67000.0,
            now=now,
            kline_closes=_calm_closes(),
            edge_threshold=0.03,
        )
        self.assertIsNotNone(opportunity)
        assert opportunity is not None
        # σ must be at least 2× the market-implied level.
        implied = implied_volatility(0.9, opportunity.t_remaining)
        self.assertGreaterEqual(opportunity.sigma, 2.0 * implied)
        # And the model probability is pulled toward 0.5
        # relative to the market's 0.90.
        self.assertLess(opportunity.p_model, 0.9)


if __name__ == "__main__":
    unittest.main()
