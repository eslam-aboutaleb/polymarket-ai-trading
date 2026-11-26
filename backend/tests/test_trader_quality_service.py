"""Unit tests for trader quality scoring.

The four sub-scores are pure functions, so they are asserted directly against
their documented bands. The persistence helpers are exercised against a real
in-memory SQLite session so the ORM interaction is genuinely covered.
"""

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.market_data.winner import Winner
from app.services.analytics.trader_quality_service import (
    _activity_score,
    _consistency_score,
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


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Winner.__table__.create(engine)
    return sessionmaker(bind=engine)()


def _strong(**overrides):
    values = {
        "wallet_address": "0xstrong",
        "win_rate": 55,
        "total_pnl": 3000,
        "volume": 30000,
        "trade_count": 300,
        "markets_traded": 80,
    }
    return _winner(**{**values, **overrides})


class ComputeQualityScoreTests(unittest.TestCase):
    """Composite score is net-of-cost; win rate has no weight."""

    def test_strong_net_return_scores_well(self):
        result = compute_quality_score(_strong())
        self.assertEqual(result["status"], "scored")
        self.assertGreater(result["quality_score"], 55)
        self.assertEqual(result["input_source"], "leaderboard_aggregates")

    def test_high_win_rate_without_net_profit_is_low_tier(self):
        favourites = _winner(
            win_rate=96, total_pnl=0, volume=50000, trade_count=500, markets_traded=90
        )
        result = compute_quality_score(favourites)
        self.assertEqual(result["status"], "scored")
        self.assertLess(result["quality_score"], 20)
        self.assertEqual(result["quality_tier"], "D")
        self.assertIsNone(result["win_rate_score"])

    def test_win_rate_does_not_move_the_score(self):
        low = compute_quality_score(_strong(win_rate=30))
        high = compute_quality_score(_strong(win_rate=95))
        self.assertEqual(low["quality_score"], high["quality_score"])

    def test_small_sample_is_unranked(self):
        result = compute_quality_score(
            _winner(win_rate=100, total_pnl=900, volume=1000, trade_count=5, markets_traded=5)
        )
        self.assertEqual(result["status"], "insufficient_sample")
        self.assertIsNone(result["quality_score"])
        self.assertEqual(result["quality_tier"], "UNRANKED")
        self.assertTrue(result["reasons"])

    def test_none_fields_do_not_crash(self):
        result = compute_quality_score(
            Winner(wallet_address="0x1", win_rate=None, total_pnl=None, volume=None)
        )
        self.assertIsNone(result["quality_score"])
        self.assertEqual(result["quality_tier"], "UNRANKED")

    def test_composite_is_bounded(self):
        result = compute_quality_score(_strong())
        self.assertGreaterEqual(result["quality_score"], 0.0)
        self.assertLessEqual(result["quality_score"], 100.0)


class PersistenceTests(unittest.TestCase):
    """Database-backed scoring against a real in-memory SQLite session."""

    def test_score_all_traders_writes_scored_and_unranked(self):
        db = _session()
        db.add_all([_strong(), _winner(wallet_address="0xtiny", markets_traded=6, volume=300)])
        db.commit()

        self.assertEqual(score_all_traders(db), 2)

        strong = db.query(Winner).filter_by(wallet_address="0xstrong").one()
        tiny = db.query(Winner).filter_by(wallet_address="0xtiny").one()
        self.assertIsNotNone(strong.quality_score)
        self.assertEqual(tiny.quality_tier, "UNRANKED")
        self.assertIsNone(tiny.quality_score)
        self.assertIsNotNone(tiny.quality_updated_at)

    def test_score_all_traders_respects_min_trades(self):
        db = _session()
        db.add(_strong())
        db.commit()
        self.assertEqual(score_all_traders(db, min_trades=1000), 0)

    def test_get_quality_returns_none_for_unknown_wallet(self):
        self.assertIsNone(get_trader_quality(_session(), "0xdeadbeef"))

    def test_get_quality_replaces_a_stale_stored_score(self):
        db = _session()
        winner = _strong()
        winner.quality_score = 10.0
        winner.quality_updated_at = datetime.now(UTC) - timedelta(hours=2)
        db.add(winner)
        db.commit()

        result = get_trader_quality(db, "0xSTRONG")

        self.assertNotEqual(result["quality_score"], 10.0)
        self.assertEqual(db.query(Winner).one().quality_score, result["quality_score"])


if __name__ == "__main__":
    unittest.main()
