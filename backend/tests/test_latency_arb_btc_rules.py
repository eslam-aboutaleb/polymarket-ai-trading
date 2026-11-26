"""BTC rule guards (issue #153), including the 8 disagreement cases from the Nov 13 report."""

import json
from pathlib import Path

import pytest

from app.services.trading import latency_arb_btc_rules as rules

REPORT = Path(__file__).resolve().parents[2] / "user guide/acceptance-replay-2026-11-13/report.json"


def late(**kw):
    base = {
        "seconds_remaining": 60.0,
        "distance": 0.004,
        "sigma_per_window": 0.002,
        "fraction_remaining": 0.1,
    }
    return rules.late_window_decision(**{**base, **kw})


def test_eight_replay_disagreements_are_all_skipped_by_the_basis_guard():
    cases = json.loads(REPORT.read_text())["oracle_counterexamples"]
    assert len(cases) == 8
    for case in cases:
        opened = float(case["external_open"])
        closed = float(case["external_final_minute_close"])
        distance = (closed - opened) / opened
        assert distance > 0  # exchange candle rose
        assert case["actual_polymarket_winner"] == "Down"  # official outcome disagreed
        assert rules.basis_risk_skip(distance), case["symbol"]
        decision = late(distance=distance, sigma_per_window=0.0005)
        assert decision.enter is False and decision.reason == "WITHIN_ORACLE_BASIS"


def test_late_window_enters_only_when_late_and_far_from_open():
    assert late().enter is True
    assert late(seconds_remaining=600).reason == "TOO_EARLY_IN_WINDOW"
    assert late(distance=0.0008).reason == "DISTANCE_NOT_LARGE_VS_SIGMA"
    assert late(distance=-0.004).enter is True  # down moves count too


@pytest.mark.parametrize(
    "kw",
    [
        {"seconds_remaining": 0.0},
        {"seconds_remaining": -5.0},
        {"sigma_per_window": 0.0},
        {"fraction_remaining": 0.0},
        {"distance": float("nan")},
        {"sigma_per_window": float("inf")},
    ],
)
def test_bad_inputs_abstain(kw):
    assert late(**kw).enter is False


def test_basis_floor_is_configurable_and_invalid_floor_skips():
    assert rules.basis_risk_skip(0.0003, basis_floor=0.0001) is False
    assert rules.basis_risk_skip(0.0003, basis_floor=0.001) is True
    assert rules.basis_risk_skip(0.01, basis_floor=-1) is True


def test_bucket_report_counts_by_time_left_and_never_picks_a_bucket():
    out = rules.bucket_report([(30, True), (30, False), (120, True), (250, False), (900, True)])
    assert out["last_1m"] == {"n": 2, "hit_rate": 0.5}
    assert out["last_3m"] == {"n": 3, "hit_rate": pytest.approx(2 / 3)}
    assert out["last_5m"]["n"] == 4
    assert rules.bucket_report([])["last_1m"] == {"n": 0, "hit_rate": None}
