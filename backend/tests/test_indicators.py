"""Unit tests for the technical-indicator maths.

These functions are pure, so the tests assert exact values and the documented
edge cases (insufficient data, invalid periods, flat series) rather than mocking
anything.
"""

import unittest

from app.utils.indicators import (
    bollinger_bands,
    compute_all_indicators,
    ema,
    generate_indicator_summary,
    macd,
    rsi,
    sma,
)


def _series(values):
    """Shorthand for a price series."""
    return [float(v) for v in values]


class SmaTests(unittest.TestCase):
    """Simple Moving Average."""

    def test_returns_none_for_insufficient_data(self):
        # Period 3 needs three prices before the first average exists.
        self.assertEqual(sma([1.0, 2.0], 3), [None, None])

    def test_first_value_seeds_from_window(self):
        self.assertEqual(sma([1.0, 2.0, 3.0], 3), [None, None, 2.0])

    def test_uses_rolling_window(self):
        # 4-period SMA over 1..5
        self.assertEqual(
            sma([1.0, 2.0, 3.0, 4.0, 5.0], 4),
            [None, None, None, 2.5, 3.5],
        )

    def test_length_matches_input(self):
        prices = _series(range(10))
        self.assertEqual(len(sma(prices, 3)), len(prices))

    def test_rejects_period_below_one(self):
        with self.assertRaises(ValueError):
            sma([1.0, 2.0, 3.0], 0)

    def test_empty_series(self):
        self.assertEqual(sma([], 3), [])


class EmaTests(unittest.TestCase):
    """Exponential Moving Average."""

    def test_returns_none_for_insufficient_data(self):
        self.assertEqual(ema([1.0, 2.0], 5), [None, None])

    def test_seeds_from_sma_of_initial_window(self):
        # Seed for period 3 over 1,2,3 is 2.0, then k = 2/4 = 0.5
        self.assertEqual(ema([1.0, 2.0, 3.0, 4.0], 3), [None, None, 2.0, 3.0])

    def test_flat_series_is_constant(self):
        self.assertEqual(ema([5.0] * 6, 3), [None, None, 5.0, 5.0, 5.0, 5.0])

    def test_rejects_period_below_one(self):
        with self.assertRaises(ValueError):
            ema([1.0], 0)


class RsiTests(unittest.TestCase):
    """Relative Strength Index (Wilder smoothing)."""

    def test_needs_period_plus_one_prices(self):
        # Three prices with period 3 cannot produce a value: RSI needs one
        # delta more than the smoothing period.
        self.assertEqual(rsi([1.0, 2.0, 3.0], period=3), [None, None, None])

    def test_pure_uptrend_is_100(self):
        rising = _series(range(1, 20))
        values = rsi(rising, period=14)
        self.assertEqual(values[14], 100.0)

    def test_pure_downtrend_is_zero(self):
        falling = _series(range(20, 1, -1))
        values = rsi(falling, period=14)
        self.assertEqual(values[14], 0.0)

    def test_stays_within_bounds(self):
        mixed = [50, 51, 49, 52, 48, 53, 47, 54, 46, 55, 45, 56, 44, 57, 43, 58]
        for value in rsi(mixed, period=14):
            if value is not None:
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 100.0)

    def test_length_matches_input(self):
        self.assertEqual(len(rsi(_series(range(30)), period=14)), 30)

    def test_rejects_period_below_one(self):
        with self.assertRaises(ValueError):
            rsi([1.0, 2.0], period=0)


class MacdTests(unittest.TestCase):
    """Moving Average Convergence Divergence."""

    def test_returns_all_three_series(self):
        prices = _series(range(1, 60))
        result = macd(prices)
        self.assertEqual(set(result), {"macd", "signal", "histogram"})

    def test_series_align_with_input(self):
        prices = _series(range(1, 60))
        result = macd(prices)
        for key in result:
            self.assertEqual(len(result[key]), len(prices))

    def test_macd_positive_in_uptrend(self):
        # Fast EMA leads a rising series, so MACD must be positive.
        prices = [float(v) for v in range(1, 80)]
        macd_line = [v for v in macd(prices)["macd"] if v is not None]
        self.assertGreater(macd_line[-1], 0.0)

    def test_macd_negative_in_downtrend(self):
        prices = [float(v) for v in range(80, 1, -1)]
        macd_line = [v for v in macd(prices)["macd"] if v is not None]
        self.assertLess(macd_line[-1], 0.0)

    def test_histogram_is_difference_of_macd_and_signal(self):
        prices = _series(range(1, 80))
        result = macd(prices)
        for m, s, h in zip(result["macd"], result["signal"], result["histogram"], strict=False):
            if h is not None and m is not None and s is not None:
                self.assertAlmostEqual(h, m - s, places=9)

    def test_short_series_yields_all_none(self):
        result = macd([1.0, 2.0])
        self.assertTrue(all(v is None for v in result["macd"]))


class BollingerBandsTests(unittest.TestCase):
    """Bollinger Bands."""

    def test_returns_upper_middle_lower(self):
        result = bollinger_bands(_series(range(1, 30)))
        self.assertEqual(set(result), {"upper", "middle", "lower"})

    def test_bands_straddle_the_middle(self):
        prices = _series(range(1, 30))
        result = bollinger_bands(prices)
        for upper, middle, lower in zip(
            result["upper"], result["middle"], result["lower"], strict=False
        ):
            if upper is None:
                continue
            self.assertGreaterEqual(upper, middle)
            self.assertLessEqual(lower, middle)

    def test_zero_variance_collapses_the_bands(self):
        prices = [7.0] * 25
        result = bollinger_bands(prices)
        for upper, middle, lower in zip(
            result["upper"], result["middle"], result["lower"], strict=False
        ):
            if upper is None:
                continue
            self.assertAlmostEqual(upper, middle)
            self.assertAlmostEqual(lower, middle)

    def test_length_matches_input(self):
        self.assertEqual(len(bollinger_bands(_series(range(40)))["upper"]), 40)


class ComputeAllIndicatorsTests(unittest.TestCase):
    """Bundle helper that feeds LLM prompts and the backtest engine."""

    def test_reports_price_count_and_latest_price(self):
        result = compute_all_indicators(_series(range(1, 60)))
        self.assertEqual(result["price_count"], 59)
        self.assertEqual(result["latest_price"], 59.0)

    def test_empty_series_has_no_latest_price(self):
        self.assertIsNone(compute_all_indicators([])["latest_price"])

    def test_echoes_requested_periods(self):
        result = compute_all_indicators(_series(range(1, 60)), sma_period=5, ema_period=7)
        self.assertEqual(result["sma"]["period"], 5)
        self.assertEqual(result["ema"]["period"], 7)

    def test_latest_is_the_last_non_null_value(self):
        # A series longer than every period means the tail is populated.
        result = compute_all_indicators(_series(range(1, 200)))
        for key in ("sma", "ema", "rsi"):
            self.assertIsNotNone(result[key]["latest"])

    def test_all_none_returns_none_latest(self):
        # Too few prices for any indicator to produce a value.
        result = compute_all_indicators([1.0, 2.0])
        self.assertIsNone(result["sma"]["latest"])


class GenerateIndicatorSummaryTests(unittest.TestCase):
    """Renders indicators as prose for LLM prompts."""

    def _summary_for(self, prices):
        return generate_indicator_summary(compute_all_indicators(prices))

    def test_always_reports_current_price(self):
        self.assertIn("Current Price", self._summary_for([1.0, 2.0]))

    def test_rising_series_reads_as_trending_up(self):
        text = self._summary_for([float(v) for v in range(1, 200)])
        self.assertIn("price is above", text)

    def test_falling_series_reads_as_trending_down(self):
        text = self._summary_for([float(v) for v in range(200, 1, -1)])
        self.assertIn("price is below", text)

    def test_reports_macd_and_rsi_zones(self):
        text = self._summary_for([float(v) for v in range(1, 200)])
        self.assertIn("RSI(", text)
        self.assertIn("MACD:", text)

    def test_rsi_zone_labels(self):
        for value, zone in ((75.0, "overbought"), (25.0, "oversold"), (50.0, "neutral")):
            text = generate_indicator_summary(
                {
                    "latest_price": 0.5,
                    "rsi": {"period": 14, "latest": value},
                }
            )
            self.assertIn(zone, text)

    def test_macd_histogram_sign_sets_bias(self):
        bullish = generate_indicator_summary(
            {
                "latest_price": 0.5,
                "macd": {
                    "latest_macd": 0.1,
                    "latest_signal": 0.05,
                    "latest_histogram": 0.05,
                },
            }
        )
        self.assertIn("bullish", bullish)
        bearish = generate_indicator_summary(
            {
                "latest_price": 0.5,
                "macd": {
                    "latest_macd": -0.1,
                    "latest_signal": 0.05,
                    "latest_histogram": -0.15,
                },
            }
        )
        self.assertIn("bearish", bearish)

    def test_bollinger_position(self):
        for price, expected in ((0.9, "near upper"), (0.1, "near lower")):
            text = generate_indicator_summary(
                {
                    "latest_price": price,
                    "bollinger_bands": {
                        "period": 20,
                        "num_std": 2.0,
                        "latest_upper": 0.95,
                        "latest_middle": 0.5,
                        "latest_lower": 0.05,
                    },
                }
            )
            self.assertIn(expected, text)

    def test_skips_sections_without_values(self):
        text = generate_indicator_summary({"latest_price": 0.5})
        self.assertNotIn("RSI(", text)
        self.assertNotIn("MACD:", text)


if __name__ == "__main__":
    unittest.main()
