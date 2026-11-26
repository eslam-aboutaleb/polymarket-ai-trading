from datetime import timedelta
from decimal import Decimal

from trader_eval_fixtures import T0, favourite_buyer, position, steady_winner

from app.services.analytics.trader_scoring import (
    MIN_RESOLVED_MARKETS,
    max_drawdown,
    score_aggregates,
    score_resolved_positions,
    shrink,
    tier_for,
)


def test_high_win_rate_but_no_net_profit_scores_low():
    fav = score_resolved_positions(favourite_buyer())
    winner = score_resolved_positions(steady_winner())
    assert fav["status"] == "scored"
    assert fav["win_rate"] == 0.95
    assert fav["win_rate_weight"] == 0
    assert fav["score"] is not None and fav["score"] < 20
    assert winner["win_rate"] < fav["win_rate"]
    assert winner["score"] > fav["score"] + 25
    assert fav["tier"] in {"D"}


def test_win_rate_alone_never_changes_score():
    base = steady_winner()
    flipped = [
        position(i, "100", "0.01" if p.pnl > 0 else "-0.01", day=i) for i, p in enumerate(base)
    ]
    a, b = score_resolved_positions(base), score_resolved_positions(flipped)
    assert a["win_rate"] == b["win_rate"]
    assert a["score"] > b["score"]


def test_below_minimum_sample_is_unranked_without_score():
    result = score_resolved_positions(
        [position(i, "100", "50") for i in range(MIN_RESOLVED_MARKETS - 1)]
    )
    assert result["status"] == "insufficient_sample"
    assert result["score"] is None
    assert result["tier"] == "UNRANKED"
    assert result["reasons"]


def test_tiny_dollar_amounts_are_unranked_even_with_many_markets():
    result = score_resolved_positions([position(i, "1", "1") for i in range(60)])
    assert result["status"] == "insufficient_sample"
    assert any("risked" in reason for reason in result["reasons"])


def test_shrinkage_favours_longer_record_with_same_raw_return():
    short = score_resolved_positions([position(i, "100", "10") for i in range(20)])
    long = score_resolved_positions([position(i, "100", "10") for i in range(200)])
    assert short["net_return_per_dollar_raw"] == long["net_return_per_dollar_raw"] == 0.1
    assert short["net_return_per_dollar_shrunk"] < long["net_return_per_dollar_shrunk"]
    assert shrink(0.1, 0) == 0.0
    assert shrink(0.1, 30) == 0.05


def test_multiple_positions_in_one_market_count_once():
    rows = [position(0, "50", "5", day=i) for i in range(10)]
    assert score_resolved_positions(rows)["distinct_resolved_markets"] == 1


def test_max_drawdown_is_peak_to_trough_in_close_order():
    rows = [
        position(0, "10", "30", day=1),
        position(1, "10", "-20", day=2),
        position(2, "10", "-15", day=3),
        position(3, "10", "40", day=4),
    ]
    assert max_drawdown(rows) == Decimal(35)
    # input order must not matter
    assert max_drawdown(list(reversed(rows))) == Decimal(35)
    assert max_drawdown([]) == Decimal(0)


def test_drawdown_lowers_score_for_same_total_return():
    smooth = [position(i, "100", "8", day=i) for i in range(40)]
    lumpy = [position(i, "100", "-60" if i < 10 else "+68", day=i) for i in range(40)]
    # both end near the same total; lumpy has a deep early drawdown
    s, g = score_resolved_positions(smooth), score_resolved_positions(lumpy)
    assert Decimal(g["max_drawdown"]) > Decimal(s["max_drawdown"])
    assert g["components"]["drawdown_score"] < s["components"]["drawdown_score"]


def test_aggregates_have_no_drawdown_credit_and_are_labeled():
    result = score_aggregates(total_pnl=2000, volume=20000, markets_traded=80, win_rate_pct=60)
    assert result["status"] == "scored"
    assert result["input_source"] == "leaderboard_aggregates"
    assert result["max_drawdown"] is None
    assert result["components"]["drawdown_score"] == 0
    assert result["score"] <= 80


def test_aggregates_handle_zero_volume_and_none():
    result = score_aggregates(total_pnl=0, volume=0, markets_traded=0, win_rate_pct=None)
    assert result["status"] == "insufficient_sample"
    assert result["net_return_per_dollar_raw"] is None


def test_negative_return_scores_zero_components():
    result = score_resolved_positions([position(i, "100", "-5") for i in range(40)])
    assert result["components"]["return_score"] == 0
    assert result["components"]["drawdown_score"] == 0


def test_tiers():
    assert tier_for(None) == "UNRANKED"
    assert tier_for(90) == "S"
    assert tier_for(0) == "D"
    assert (T0 + timedelta(days=1)).tzinfo is not None
