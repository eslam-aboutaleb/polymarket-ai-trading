"""Unit tests for trader quality scoring.

The four sub-scores are pure functions, so they are asserted directly against
their documented bands. The persistence helpers are exercised against a real
in-memory SQLite session so the ORM interaction is genuinely covered.
"""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.models.winner import Winner
from app.services.trader_quality_service import (
    _activity_score,
    _consistency_score,
    _risk_adjusted_score,
    _win_rate_score,
    compute_quality_score,
    get_trader_quality,
    score_all_traders,
)


def _winner(**overrides):
    """Build a Winner populated with safe defaults for scoring."""
    defaults = {
        "wallet_address": "0xabc",
        "win_rate": 50.0,
        "total_pnl": 1000.0,
        "volume": 10000.0,
        "volume_24h": 500.0,
        "trade_count": 30,
        "markets_traded": 10,
        "pnl_24h": 100.0,
        "pnl_7d": 200.0,
        "pnl_30d": 1000.0,
        "last_trade_time": datetime.now(UTC),
    }
    return Winner(**{**defaults, **overrides})


class WinRateScoreTests(unittest.TestCase):
    """Win-rate band scoring."""

    def test_zero_and_negative_score_zero(self):
        self.assertEqual(_win_rate_score(0), 0.0)
        self.assertEqual(_win_rate_score(-5), 0.0)

    def test_top_band_is_capped_at_100(self):
        self.assertEqual(_win_rate_score(80), 100.0)
        self.assertEqual(_win_rate_score(95), 100.0)

    def test_bands_increase_monotonically(self):
        scores = [_win_rate_score(wr) for wr in (45, 55, 65, 75, 85)]
        self.assertEqual(scores, sorted(scores))

    def test_mid_band_interpolates(self):
        # 75% sits two-thirds into the 70-80 band: 85 + 5*1.5 = 92.5
        self.assertAlmostEqual(_win_rate_score(75), 92.5)

    def test_band_edges_score_their_base_value(self):
        self.assertEqual(_win_rate_score(80), 100.0)
        self.assertEqual(_win_rate_score(70), 85.0)
        self.assertEqual(_win_rate_score(60), 70.0)
        self.assertEqual(_win_rate_score(50), 50.0)

    def test_below_the_lowest_band_is_scaled_one_to_one(self):
        self.assertEqual(_win_rate_score(45), 45.0)

    def test_missing_win_rate_scores_zero(self):
        self.assertEqual(_win_rate_score(None), 0.0)


class ConsistencyScoreTests(unittest.TestCase):
    """P&L consistency across periods."""

    def test_all_periods_positive_scores_highest(self):
        self.assertGreaterEqual(_consistency_score(100, 200, 1000, 1300), 80.0)

    def test_two_positive_periods_is_mid_band(self):
        self.assertGreaterEqual(_consistency_score(100, -50, 200, 250), 60.0)

    def test_no_positive_periods_and_negative_total_is_low(self):
        self.assertEqual(_consistency_score(-1, -2, -3, -10), 0.0)

    def test_result_stays_in_bounds(self):
        for args in [(1, 1, 1, 1), (-1, -1, -1, -1), (0, 0, 0, 0)]:
            score = _consistency_score(*args)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 100.0)

    def test_never_exceeds_100(self):
        # Bonus stacking can overshoot; the function must clamp.
        self.assertLessEqual(_consistency_score(500, 200, 500, 2000), 100.0)

    def test_none_values_treated_as_zero(self):
        self.assertEqual(_consistency_score(None, None, None, None), 0.0)

    def test_single_positive_period_scores_forty(self):
        # Exactly one of the three periods is positive, so the 40-point band
        # applies and the weekly-share bonus is skipped (pnl_7d is falsy).
        self.assertEqual(_consistency_score(100, 0, 0, 500), 40.0)

    def test_no_positive_periods_with_a_positive_total_scores_twenty_five(self):
        # Every window is negative but the all-time P&L is up: the trader is
        # inconsistent rather than unprofitable.
        self.assertEqual(_consistency_score(-10, -20, -30, 500), 25.0)

    def test_weekly_share_inside_the_strong_band_adds_fifteen(self):
        # pnl_7d / pnl_30d == 0.2 sits inside the 0.15-0.35 band.
        self.assertEqual(_consistency_score(10, 20, 100, 200), 95.0)

    def test_weekly_share_inside_the_loose_band_adds_eight(self):
        # 10/100 == 0.1 is inside the wider 0.05-0.5 band but not the strong one.
        self.assertEqual(_consistency_score(10, 10, 100, 200), 88.0)

    def test_weekly_share_outside_both_bands_adds_nothing(self):
        self.assertEqual(_consistency_score(10, 90, 100, 200), 80.0)

    def test_negative_thirty_day_total_skips_the_share_bonus(self):
        # The bonus is guarded on a positive 30-day P&L, so a negative ratio
        # cannot subtract from the base score (here: one positive period).
        self.assertEqual(_consistency_score(10, -5, -100, 200), 40.0)


class RiskAdjustedScoreTests(unittest.TestCase):
    """PnL-per-volume efficiency."""

    def test_zero_volume_scores_zero(self):
        self.assertEqual(_risk_adjusted_score(1000, 0), 0.0)

    def test_zero_pnl_scores_zero(self):
        self.assertEqual(_risk_adjusted_score(0, 10000), 0.0)

    def test_high_efficiency_caps_at_100(self):
        self.assertEqual(_risk_adjusted_score(2000, 10000), 100.0)

    def test_efficiency_boundaries_are_inclusive(self):
        # Each band starts exactly on its threshold and scores its base value.
        self.assertEqual(_risk_adjusted_score(1500, 10000), 100.0)
        self.assertEqual(_risk_adjusted_score(1000, 10000), 85.0)
        self.assertEqual(_risk_adjusted_score(500, 10000), 70.0)
        self.assertEqual(_risk_adjusted_score(200, 10000), 50.0)
        self.assertEqual(_risk_adjusted_score(100, 10000), 35.0)

    def test_small_positive_efficiency_is_scaled_by_thirty_five_hundred(self):
        # 0.5% efficiency is below every band: 0.005 * 3500.
        self.assertAlmostEqual(_risk_adjusted_score(50, 10000), 17.5)

    def test_mild_loss_penalises_but_stays_positive(self):
        # A -1% efficiency still scores: 20 + (-0.01 * 200).
        self.assertEqual(_risk_adjusted_score(-100, 10000), 18.0)

    def test_deep_loss_is_floored_at_zero(self):
        # -20% efficiency drives the raw score negative, so it must clamp.
        self.assertEqual(_risk_adjusted_score(-2000, 10000), 0.0)

    def test_losses_are_penalised_but_not_negative(self):
        self.assertGreaterEqual(_risk_adjusted_score(-5000, 10000), 0.0)

    def test_efficiency_increases_with_pnl(self):
        low = _risk_adjusted_score(500, 10000)
        high = _risk_adjusted_score(1200, 10000)
        self.assertGreater(high, low)

    def test_efficiency_bands_increase_monotonically(self):
        # One point inside each band of the efficiency curve.
        efficiencies = [50, 150, 300, 600, 1200, 2000]
        scores = [_risk_adjusted_score(e, 10000) for e in efficiencies]
        self.assertEqual(scores, sorted(scores))

    def test_very_small_but_positive_efficiency_is_low(self):
        self.assertLess(_risk_adjusted_score(5, 10000), 30.0)


class ActivityScoreTests(unittest.TestCase):
    """Trade count, breadth, volume and freshness."""

    def test_highly_active_trader_nears_ceiling(self):
        score = _activity_score(
            trade_count=600,
            markets_traded=60,
            volume_24h=20000,
            last_trade_time=datetime.now(UTC),
        )
        self.assertGreaterEqual(score, 95.0)

    def test_inactive_trader_scores_low(self):
        score = _activity_score(0, 0, 0, None)
        self.assertLess(score, 20.0)

    def test_recent_activity_beats_stale(self):
        fresh = _activity_score(10, 2, 50, datetime.now(UTC))
        stale = _activity_score(10, 2, 50, datetime.now(UTC) - timedelta(days=60))
        self.assertGreater(fresh, stale)

    def test_volume_without_timestamp_still_earns_points(self):
        with_volume = _activity_score(5, 1, 5000, None)
        without = _activity_score(5, 1, 0, None)
        self.assertGreater(with_volume, without)

    def test_capped_at_100(self):
        score = _activity_score(10_000, 1_000, 10_000_000, datetime.now(UTC))
        self.assertLessEqual(score, 100.0)

    def test_trade_count_bands_increase(self):
        now = datetime.now(UTC)
        scores = [_activity_score(t, 1, 0, now) for t in (5, 30, 60, 120, 250, 600)]
        self.assertEqual(scores, sorted(scores))

    def test_markets_traded_bands_increase(self):
        now = datetime.now(UTC)
        scores = [_activity_score(1, m, 0, now) for m in (1, 6, 12, 25, 60)]
        self.assertEqual(scores, sorted(scores))

    def test_freshness_bands_decrease_with_age(self):
        now = datetime.now(UTC)
        ages = [_activity_score(1, 1, 0, now - timedelta(days=d)) for d in (0, 2, 5, 10, 20)]
        self.assertEqual(ages, sorted(ages, reverse=True))

    def test_volume_bands_increase(self):
        now = datetime.now(UTC)
        scores = [_activity_score(1, 1, v, now) for v in (10, 200, 5000, 50000)]
        self.assertEqual(scores, sorted(scores))


class ComputeQualityScoreTests(unittest.TestCase):
    """Composite score and tier assignment."""

    def test_strong_trader_lands_in_top_tier(self):
        result = compute_quality_score(
            _winner(
                win_rate=85,
                total_pnl=5000,
                volume=20000,
                volume_24h=8000,
                trade_count=400,
                markets_traded=45,
            )
        )
        self.assertIn(result["quality_tier"], {"S", "A"})
        self.assertGreater(result["quality_score"], 70)

    def test_weak_trader_lands_in_low_tier(self):
        result = compute_quality_score(
            _winner(
                win_rate=5,
                total_pnl=-200,
                volume=20000,
                volume_24h=0,
                trade_count=1,
                markets_traded=1,
                last_trade_time=None,
            )
        )
        self.assertEqual(result["quality_tier"], "D")

    def test_returns_all_five_components(self):
        result = compute_quality_score(_winner())
        for key in (
            "quality_score",
            "win_rate_score",
            "consistency_score",
            "risk_adjusted_score",
            "activity_score",
        ):
            self.assertIn(key, result)

    def test_none_fields_do_not_crash(self):
        result = compute_quality_score(
            Winner(wallet_address="0x1", win_rate=None, total_pnl=None, volume=None)
        )
        self.assertEqual(result["quality_score"], 0.0)

    def test_composite_is_bounded(self):
        result = compute_quality_score(_winner())
        self.assertGreaterEqual(result["quality_score"], 0.0)
        self.assertLessEqual(result["quality_score"], 100.0)


class PersistenceTests(unittest.TestCase):
    """Database-backed scoring helpers, using a mocked session."""

    def test_score_all_traders_writes_every_component(self):
        winner = _winner()
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [winner]

        count = score_all_traders(db)

        self.assertEqual(count, 1)
        self.assertIsNotNone(winner.quality_score)
        self.assertIsNotNone(winner.quality_tier)
        self.assertIsNotNone(winner.quality_updated_at)
        db.commit.assert_called_once()

    def test_score_all_traders_respects_min_trades(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        self.assertEqual(score_all_traders(db, min_trades=100), 0)

    def test_get_quality_returns_none_for_unknown_wallet(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        self.assertIsNone(get_trader_quality(db, "0xdeadbeef"))

    def test_get_quality_computes_when_missing(self):
        winner = _winner()
        winner.quality_score = None
        winner.quality_updated_at = None
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = winner

        result = get_trader_quality(db, "0xabc")

        self.assertIn("quality_score", result)
        db.commit.assert_called_once()

    def test_get_quality_computes_when_stale(self):
        winner = _winner()
        winner.quality_score = 10.0
        winner.quality_updated_at = datetime.now(UTC) - timedelta(hours=2)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = winner

        get_trader_quality(db, "0xabc")

        # The stale stored score must have been replaced.
        self.assertNotEqual(winner.quality_score, 10.0)

    def test_get_quality_returns_cached_when_fresh(self):
        winner = _winner()
        winner.quality_score = 55.5
        winner.quality_updated_at = datetime.now(UTC)
        winner.consistency_score = 50.0
        winner.risk_adjusted_score = 45.0
        winner.activity_score = 40.0
        winner.quality_tier = "B"
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = winner

        result = get_trader_quality(db, "0xabc")

        self.assertEqual(result["quality_score"], 55.5)
        db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
