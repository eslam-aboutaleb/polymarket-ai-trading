"""Promotion gate (issue #155): synthetic resolved trades, no database or network."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.services.trading import live_trading_policy as policy
from app.services.trading import promotion_gate as pg

T0 = datetime(2026, 11, 1, tzinfo=UTC)
PARAMS = {"edge_threshold": 0.03, "max_usd": 50}


def trades(n, mean, params=PARAMS, seed=1, fee=0.01, spread=0.05, start=T0, **kw):
    rng = random.Random(seed)  # noqa: S311
    digest = pg.freeze_parameters(params)
    return [
        pg.ResolvedTrade(
            resolved_at=start + timedelta(minutes=i),
            params_hash=digest,
            gross_return=mean + fee + rng.uniform(-spread, spread),
            fee_per_dollar=fee,
            **kw,
        )
        for i in range(n)
    ]


def window(params=PARAMS):
    return pg.open_window("latency_arb", params, T0)


def test_hash_ignores_key_order_and_changes_with_any_value():
    assert pg.freeze_parameters({"a": 1, "b": 2}) == pg.freeze_parameters({"b": 2, "a": 1})
    assert pg.freeze_parameters({"a": 1}) != pg.freeze_parameters({"a": 2})


def test_changing_parameters_restarts_the_clock_and_same_parameters_do_not():
    first = pg.open_window("latency_arb", PARAMS, T0)
    later = T0 + timedelta(days=3)
    assert pg.open_window("latency_arb", dict(PARAMS), later, previous=first) is first
    changed = pg.open_window("latency_arb", {**PARAMS, "edge_threshold": 0.04}, later, first)
    assert changed.started_at == later and changed.params_hash != first.params_hash


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError):
        pg.open_window("yolo", PARAMS, T0)


def test_passes_with_enough_trades_and_ci_lower_bound_above_zero():
    d = pg.evaluate_promotion(window(), trades(400, 0.03))
    assert d.passed and d.reasons == ()
    assert d.n == 400 and d.ci_low > 0 and d.ci_low < d.mean_net_return < d.ci_high
    policy.require_promotion_gate("latency_arb", d)  # does not raise


def test_refused_when_sample_below_minimum():
    d = pg.evaluate_promotion(window(), trades(50, 0.05))
    assert not d.passed
    assert d.reasons[0] == "INSUFFICIENT_SAMPLE: 50 < 300"
    with pytest.raises(policy.LiveTradingBlocked, match="INSUFFICIENT_SAMPLE: 50 < 300"):
        policy.require_promotion_gate("latency_arb", d)


def test_refused_when_ci_lower_bound_not_above_zero_even_if_mean_is_positive():
    d = pg.evaluate_promotion(window(), trades(400, 0.01, spread=0.4))
    assert d.mean_net_return > 0
    assert d.ci_low <= 0
    assert "CI_LOWER_BOUND_NOT_ABOVE_ZERO" in d.reasons
    with pytest.raises(policy.LiveTradingBlocked, match="CI_LOWER_BOUND_NOT_ABOVE_ZERO"):
        policy.require_promotion_gate("latency_arb", d)


def test_refused_without_evidence_or_for_another_strategy():
    with pytest.raises(policy.LiveTradingBlocked, match="NO_EVIDENCE"):
        policy.require_promotion_gate("latency_arb", None)
    d = pg.evaluate_promotion(window(), trades(400, 0.03))
    with pytest.raises(policy.LiveTradingBlocked, match="STRATEGY_MISMATCH"):
        policy.require_promotion_gate("copy", d)


def test_trades_under_other_parameters_or_before_the_window_do_not_count():
    other = trades(500, 0.05, params={**PARAMS, "edge_threshold": 0.9})
    early = trades(500, 0.05, start=T0 - timedelta(days=30))
    mine = trades(100, 0.05)
    d = pg.evaluate_promotion(window(), other + early + mine)
    assert d.n == 100
    assert d.excluded_other_parameters == 500
    assert d.excluded_before_window == 500
    assert "PARAMETERS_CHANGED_IN_WINDOW" in d.reasons
    assert not d.passed


def test_report_shows_n_mean_ci_baseline_and_fees_separately():
    rng = random.Random(7)  # noqa: S311
    rows = []
    digest = pg.freeze_parameters(PARAMS)
    for i in range(400):
        outcome = int(rng.random() < 0.6)
        rows.append(
            pg.ResolvedTrade(T0 + timedelta(minutes=i), digest, 0.05, 0.02, 1.0, 0.6, 0.5, outcome)
        )
    d = pg.evaluate_promotion(window(), rows, pg.PromotionConfig(require_baseline=True))
    report = d.to_dict()
    for key in ("n", "mean_gross_return", "mean_fee", "mean_net_return", "ci_low", "ci_high"):
        assert report[key] is not None
    assert report["mean_gross_return"] == pytest.approx(0.05)
    assert report["mean_fee"] == pytest.approx(0.02)
    assert report["mean_net_return"] == pytest.approx(0.03)
    assert set(report["baseline"]) >= {"brier_model", "brier_market_midpoint", "log_loss_model"}
    assert "does not imply" in report["note"]
    assert len(report["evidence_sha256"]) == 64
    assert d.passed  # model 0.6 beats midpoint 0.5 against a 60% base rate


def test_forecast_strategy_must_beat_the_market_midpoint():
    digest = pg.freeze_parameters(PARAMS)
    rng = random.Random(3)  # noqa: S311
    rows = []
    for i in range(400):
        outcome = int(rng.random() < 0.5)
        rows.append(
            pg.ResolvedTrade(T0 + timedelta(minutes=i), digest, 0.05, 0.01, 1.0, 0.9, 0.5, outcome)
        )
    d = pg.evaluate_promotion(window(), rows, pg.PromotionConfig(require_baseline=True))
    assert "DOES_NOT_BEAT_MARKET_MIDPOINT" in d.reasons
    missing = pg.evaluate_promotion(
        window(), trades(400, 0.03), pg.PromotionConfig(require_baseline=True)
    )
    assert "BASELINE_COMPARISON_MISSING" in missing.reasons


def test_bootstrap_is_deterministic_and_validates_config():
    vals = [0.01, -0.02, 0.03, 0.0, 0.02] * 20
    a = pg.bootstrap_mean_ci(vals, samples=500, level=0.95, seed=1)
    assert a == pg.bootstrap_mean_ci(vals, samples=500, level=0.95, seed=1)
    with pytest.raises(ValueError):
        pg.PromotionConfig(min_trades=0)
    with pytest.raises(ValueError):
        pg.PromotionConfig(ci_level=0.4)
    with pytest.raises(ValueError):
        pg.bootstrap_mean_ci([], samples=500, level=0.95, seed=1)


# ── demotion ────────────────────────────────────────────────────────────────


def passed_decision():
    d = pg.evaluate_promotion(window(), trades(400, 0.03))
    assert d.passed
    return d


def test_demotion_on_synthetic_losing_live_results():
    cap = pg.LiveCap(max_trade_notional=5.0, max_daily_loss=10.0)
    losing = trades(40, -0.05, notional=2.0)
    demotion = pg.evaluate_demotion(passed_decision(), losing, cap)
    assert demotion.demote and demotion.reasons == ("LIVE_BELOW_PAPER_CI",)
    assert demotion.live_mean_net_return < demotion.paper_ci_low


def test_no_demotion_when_live_matches_paper_and_caps_hold():
    cap = pg.LiveCap(max_trade_notional=5.0, max_daily_loss=10.0)
    fine = trades(40, 0.04, seed=9, notional=2.0)
    assert not pg.evaluate_demotion(passed_decision(), fine, cap).demote


def test_small_live_sample_is_not_judged_but_caps_always_are():
    cap = pg.LiveCap(max_trade_notional=5.0, max_daily_loss=10.0)
    few = trades(5, -0.5, notional=2.0)
    assert not pg.evaluate_demotion(passed_decision(), few, cap).demote
    big = trades(1, 0.0, notional=50.0)
    assert pg.evaluate_demotion(passed_decision(), big, cap).reasons == (
        "LIVE_NOTIONAL_CAP_BREACHED",
    )
    stop = pg.evaluate_demotion(passed_decision(), few, cap, daily_pnl=-10.0)
    assert stop.reasons == ("DAILY_LOSS_LIMIT_HIT",)


def test_cannot_demote_a_decision_that_never_passed():
    failed = pg.evaluate_promotion(window(), trades(5, 0.05))
    with pytest.raises(ValueError):
        pg.evaluate_demotion(failed, [], pg.LiveCap(5.0, 10.0))
    with pytest.raises(ValueError):
        pg.LiveCap(0, 1)
