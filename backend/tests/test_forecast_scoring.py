"""Issue #22: proper scoring, matched-baseline skill and time-forward calibration.

Pure-function tests with hand-computed expectations. No mocks: the scorer has
no collaborators.
"""

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.services.intelligence import forecast_scoring as fs
from app.services.intelligence.forecast_scoring import PredictionScoreInput as In

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def mk(i, p, y, *, base=None, cluster=None, **kw):
    return In(
        forecast_id=f"f{i}",
        as_of=kw.pop("as_of", T0 + timedelta(hours=i)),
        rule_hash="r",
        p=p,
        baseline_p=base,
        final_label=y,
        event_cluster=cluster or f"e{i}",
        source_quality="matched" if base is not None else "unverified",
        **kw,
    )


def test_perfect_forecasts_score_zero():
    assert fs.brier(1.0, 1.0) == 0 and fs.brier(0.0, 0.0) == 0
    assert fs.log_loss(1.0, 1.0) == pytest.approx(0, abs=1e-5)


def test_constant_half_is_quarter_brier_and_ln2_log_loss():
    rows = [mk(i, 0.5, float(i % 2)) for i in range(10)]
    r = fs.evaluate(rows, resamples=50)
    assert r["brier"] == pytest.approx(0.25)
    assert r["log_loss"] == pytest.approx(math.log(2))


def test_adversarial_extremes_are_finite_and_clipping_is_reported():
    assert fs.brier(1.0, 0.0) == 1.0
    assert fs.log_loss(1.0, 0.0) == pytest.approx(-math.log(fs.LOG_LOSS_EPSILON))
    assert math.isfinite(fs.log_loss(0.0, 1.0))
    r = fs.evaluate([mk(1, 1.0, 0.0), mk(2, 0.0, 1.0), mk(3, 0.5, 1.0)], resamples=20)
    assert r["brier"] == pytest.approx((1 + 1 + 0.25) / 3)
    assert r["clipped_for_log_loss"] == 2
    assert "clipped" in r["clipping_policy"]


def test_invalid_inputs_are_rejected_not_coerced():
    for bad in (-0.1, 1.1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            fs.brier(bad, 1.0)
    with pytest.raises(ValueError):
        fs.brier(0.5, 0.5)


def test_exclusions_are_counted_by_reason_and_never_defaulted():
    close = T0 + timedelta(hours=10)
    rows = [
        mk(1, 0.7, 1.0, market_close_at=close),
        mk(2, 0.7, 1.0, market_close_at=T0),  # forecast at/after close: late
        mk(3, 0.7, None),  # unresolved / void / disputed
        mk(4, float("nan"), 1.0),
        mk(5, 0.7, 1.0, mapping_reversed=True),
        In("f6", None, "r", 0.7, None, 1.0, "e6"),
    ]
    prepared = fs.prepare(rows)
    assert [i.forecast_id for i in prepared.accepted] == ["f1"]
    assert prepared.excluded == {
        "forecast_after_close": 1,
        "no_final_label": 1,
        "invalid_probability": 1,
        "reversed_outcome_mapping": 1,
        "missing_as_of": 1,
    }


def test_reversed_outcome_mapping_would_change_the_score_so_it_is_excluded():
    right = fs.evaluate([mk(1, 0.9, 1.0)], resamples=10)["brier"]
    flipped_label = fs.evaluate([mk(1, 0.9, 0.0)], resamples=10)["brier"]
    assert right == pytest.approx(0.01) and flipped_label == pytest.approx(0.81)
    assert (
        fs.evaluate([mk(1, 0.9, 1.0, mapping_reversed=True)], resamples=10)["scored_forecast_count"]
        == 0
    )


def test_one_event_is_one_independent_trial():
    # Five overlapping markets of one event plus one single-market event.
    rows = [mk(i, 0.9, 1.0, cluster="same-event") for i in range(5)] + [
        mk(9, 0.1, 1.0, cluster="other")
    ]
    r = fs.evaluate(rows, resamples=20)
    assert r["scored_forecast_count"] == 6
    assert r["effective_independent_events"] == 2
    # Cluster means: 0.01 and 0.81 -> 0.41; row-averaging would give ~0.1417.
    assert r["brier"] == pytest.approx(0.41)


def test_reliability_bins_have_explicit_support_and_ece_sensitivity():
    rows = [mk(i, 0.85, 1.0 if i < 8 else 0.0) for i in range(10)]  # 80% observed at p=.85
    r = fs.evaluate(rows, resamples=10)
    bin8 = r["reliability"][8]
    assert bin8["count"] == 10 and bin8["observed_frequency"] == pytest.approx(0.8)
    assert bin8["ci95"][0] < 0.8 < bin8["ci95"][1]
    assert r["ece"] == pytest.approx(0.05)
    assert set(r["ece_sensitivity"]) == {"5", "10", "20"}
    assert r["calibration_in_the_large"] == pytest.approx(0.8 - 0.85)
    assert fs.reliability([], 10)[0]["mean_p"] is None


def test_paired_delta_with_matched_baseline_and_cluster_ci():
    rng = random.Random(1)  # noqa: S311
    rows = []
    for i in range(60):
        y = 1.0 if rng.random() < 0.5 else 0.0
        model = 0.8 if y else 0.2  # informative
        rows.append(mk(i, model, y, base=0.5))
    r = fs.evaluate(rows, min_effective_events=30, resamples=300)
    assert r["status"] == "better_than_market"
    assert r["paired_brier_delta"] == pytest.approx(0.25 - 0.04)
    assert r["paired_brier_delta_ci95"][0] > 0
    assert r["brier_skill"] == pytest.approx(1 - 0.04 / 0.25)
    assert r["paired_log_loss_delta"] > 0
    assert r["claims_alpha"] is False


def test_worse_than_market_and_not_distinguishable():
    bad = [mk(i, 0.2 if i % 2 else 0.8, float(i % 2), base=0.5) for i in range(40)]
    assert fs.evaluate(bad, min_effective_events=30, resamples=200)["status"] == "worse_than_market"
    rng = random.Random(7)  # noqa: S311
    same = []
    for i in range(40):
        y = float(rng.random() < 0.5)
        same.append(mk(i, 0.5 + (0.01 if i % 2 else -0.01), y, base=0.5))
    assert (
        fs.evaluate(same, min_effective_events=30, resamples=200)["status"] == "not_distinguishable"
    )


def test_low_sample_never_claims_skill_even_with_a_perfect_brier():
    rows = [mk(i, 0.99, 1.0, base=0.5) for i in range(5)]
    r = fs.evaluate(rows, min_effective_events=30, resamples=50)
    assert r["status"] == "insufficient_sample" and r["abstain_reason"] == "LOW_EFFECTIVE_SAMPLE"
    gate = fs.research_only(r, drift_flagged=False)
    assert gate["research_only"] and gate["execution_eligible"] is False


def test_unmatched_baseline_is_not_used_for_the_paired_result():
    rows = [mk(i, 0.9, 1.0) for i in range(40)]
    rows[0] = In("x", T0, "r", 0.9, 0.5, 1.0, "ex", source_quality="unverified")  # ex-post price
    r = fs.evaluate(rows, resamples=20)
    assert r["status"] == "no_matched_baseline" and r["abstain_reason"] == "NO_MATCHED_BASELINE"
    assert "paired_brier_delta" not in r


def test_empty_and_all_excluded():
    r = fs.evaluate([], resamples=10)
    assert r["abstain_reason"] == "NO_SCORABLE_FORECASTS"
    assert fs.evaluate([mk(1, 0.5, None)], resamples=10)["excluded"] == {"no_final_label": 1}


def test_bootstrap_is_deterministic_for_a_seed():
    rows = [mk(i, 0.7, float(i % 2), base=0.5) for i in range(40)]
    a = fs.evaluate(rows, resamples=100, seed=3)
    b = fs.evaluate(rows, resamples=100, seed=3)
    assert a["paired_brier_delta_ci95"] == b["paired_brier_delta_ci95"]


def test_drift_gate_forces_research_only_even_with_skill():
    rows = [mk(i, 0.8 if i % 2 else 0.2, float(i % 2), base=0.5) for i in range(60)]
    r = fs.evaluate(rows, resamples=200)
    assert r["status"] == "better_than_market"
    assert fs.research_only(r, drift_flagged=False)["research_only"] is False
    gated = fs.research_only(r, drift_flagged=True)
    assert gated["research_only"] and "MODEL_DRIFT" in gated["reasons"]


# ── time-forward calibration ──


def labelled(i, p, y, as_of, resolved_after_hours):
    return In(
        f"c{i}",
        as_of,
        "r",
        p,
        None,
        y,
        f"ev{i}",
        resolved_at=as_of + timedelta(hours=resolved_after_hours),
    )


def test_split_never_fits_on_labels_unknown_at_the_cutoff():
    cutoff = T0 + timedelta(days=10)
    early_known = labelled(1, 0.6, 1.0, T0, 24)
    early_late_label = labelled(2, 0.6, 0.0, T0 + timedelta(days=9), 72)  # resolves after cutoff
    holdout = labelled(3, 0.6, 1.0, cutoff + timedelta(hours=1), 5)
    fit, hold = fs.split_time_forward([early_known, early_late_label, holdout], cutoff)
    assert [i.forecast_id for i in fit] == ["c1"]
    assert [i.forecast_id for i in hold] == ["c3"]
    assert all(i.resolved_at <= cutoff for i in fit)
    assert min(i.as_of for i in hold) > cutoff


def test_isotonic_is_monotone_and_fixes_systematic_overconfidence():
    fit = []
    for i in range(200):
        p = 0.9 if i % 2 else 0.1
        y = 1.0 if (i % 10 < 6) == (p > 0.5) else 0.0  # 60% right either way
        fit.append(
            labelled(i, p, y if p > 0.5 else 1 - y if False else y, T0 + timedelta(hours=i), 1)
        )
    cal = fs.fit_isotonic(fit)
    xs = [k[0] for k in cal.knots]
    ys = [k[1] for k in cal.knots]
    assert xs == sorted(xs) and ys == sorted(ys)
    assert cal(0.0) == ys[0] and cal(1.0) == ys[-1]
    with pytest.raises(ValueError):
        fs.fit_isotonic([])


def test_platt_learns_shrinkage_for_an_overconfident_forecaster():
    rng = random.Random(5)  # noqa: S311
    fit = []
    for i in range(400):
        p = rng.choice([0.05, 0.95])
        true = 0.25 if p < 0.5 else 0.75  # real frequency is far less extreme
        fit.append(labelled(i, p, float(rng.random() < true), T0 + timedelta(hours=i), 1))
    cal = fs.fit_platt(fit)
    assert cal.platt[0] < 0.6  # slope well below 1: shrink
    assert 0.6 < cal(0.95) < 0.9 and 0.1 < cal(0.05) < 0.4


def test_recalibration_improves_holdout_brier_and_records_training_span():
    rng = random.Random(11)  # noqa: S311
    rows = []
    for i in range(500):
        p = rng.choice([0.05, 0.95])
        true = 0.25 if p < 0.5 else 0.75
        as_of = T0 + timedelta(hours=i)
        rows.append(
            In(
                f"r{i}",
                as_of,
                "r",
                p,
                None,
                float(rng.random() < true),
                f"e{i}",
                resolved_at=as_of + timedelta(hours=1),
            )
        )
    cutoff = T0 + timedelta(hours=300)
    out = fs.recalibrate_holdout(rows, cutoff, method="platt", min_fit_events=30, resamples=50)
    assert out["status"] == "evaluated"
    assert out["recalibrated"]["brier"] < out["raw"]["brier"]
    assert datetime.fromisoformat(out["train_span"][1]) <= cutoff
    assert out["holdout_forecasts"] == sum(1 for r in rows if r.as_of > cutoff)


def test_recalibration_abstains_without_enough_past_events():
    rows = [labelled(i, 0.9, 1.0, T0 + timedelta(hours=i), 1) for i in range(10)]
    out = fs.recalibrate_holdout(rows, T0 + timedelta(hours=5))
    assert out["status"] == "insufficient_sample"
