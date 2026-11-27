import copy
from decimal import Decimal

import pytest
from event_replay_fixtures import at, book, config, follow, settlement, signal

from app.services.backtesting.contracts import OrderIntent
from app.services.backtesting.event_replay_engine import events_snapshot_id, replay
from app.services.backtesting.shadow_runner import ShadowSession, parity, run_shadow
from app.services.backtesting.strategy_lab import benchmark_report, compare, run_baselines
from app.services.backtesting.walk_forward_validator import (
    Candidate,
    ExperimentSpec,
    HoldoutError,
    WalkForwardConfig,
    WalkForwardValidator,
    split_signals,
)

DAY = 86400


def market(i, start, *, win):
    """One binary market: book, a copy signal, then a final settlement 2 days later."""
    tok = f"tok-{i}"
    return [
        book(f"b-{i}", start, [("0.48", 500)], [("0.50", 500)], token=tok),
        signal(f"s-{i}", start + 60, token=tok, group=f"grp-{i}", source_pnl="123"),
        settlement(f"f-{i}", start + 2 * DAY, "1" if win else "0", token=tok),
    ]


def world(n=45, win_every=2):
    events = []
    for i in range(n):
        events += market(i, i * DAY, win=(i % win_every == 0))
    return events


def spec_for(events, n=45, embargo=0, candidates=None):
    cfg = WalkForwardConfig(
        train_end=at(15 * DAY),
        validation_end=at(25 * DAY),
        end=at((n + 5) * DAY),
        embargo_seconds=embargo,
    )
    return ExperimentSpec(
        experiment_id="exp1",
        snapshot_id=events_snapshot_id(events),
        walk_forward=cfg,
        config=config(),
        candidates=candidates
        or [
            Candidate(candidate_id="small", params={"amount": "10"}),
            Candidate(candidate_id="big", params={"amount": "100"}),
        ],
        candidate_tokens=[f"tok-{i}" for i in range(n)],
    )


def factory(params):
    amount = Decimal(params["amount"])

    def decide(ctx):
        e = ctx.event
        if e.kind != "SIGNAL":
            return []
        return [
            OrderIntent(
                intent_id=f"o-{e.event_id}",
                token_id=e.token_id,
                side="BUY",
                amount=amount,
                units="USDC",
            )
        ]

    return decide


def test_split_is_chronological_by_decision_time_and_disjoint():
    events = world()
    split = split_signals(events, spec_for(events).walk_forward)
    ids = {k: {e.event_id for e in split[k]} for k in ("train", "validation", "holdout")}
    assert not (ids["train"] & ids["validation"]) and not (ids["validation"] & ids["holdout"])
    assert max(e.available_at for e in split["train"]) < at(15 * DAY)
    assert min(e.available_at for e in split["validation"]) >= at(15 * DAY)
    assert min(e.available_at for e in split["holdout"]) >= at(25 * DAY)


def test_signals_whose_market_resolves_after_the_split_are_purged():
    events = world()
    split = split_signals(events, spec_for(events).walk_forward)
    reasons = {p["event_id"]: p["reason"] for p in split["purged"]}
    # Signal 14 sits just before train_end (day 15) but resolves on day 16.
    assert reasons["s-14"] == "label_resolves_after_segment"
    assert "s-14" not in {e.event_id for e in split["train"]}


def test_embargo_and_group_straddle_are_purged():
    events = world() + [signal("late-grp-3", 20 * DAY, token="tok-3", group="grp-3")]
    spec = spec_for(events, embargo=2 * DAY)
    split = split_signals(events, spec.walk_forward)
    reasons = {p["event_id"]: p["reason"] for p in split["purged"]}
    assert reasons["late-grp-3"] == "group_straddles_split"
    assert reasons["s-15"] == "embargo"


def test_tuning_ignores_holdout_and_holdout_is_consumed_once():
    events = world()
    spec = spec_for(events)
    validator = WalkForwardValidator(spec, events)
    tuned = validator.tune(factory)
    # Corrupt every holdout-era event: tuning on a fresh validator must not change.
    corrupted = [e for e in events if e.available_at < at(25 * DAY)] + [
        e.model_copy(update={"amount": Decimal(1)}) if e.kind == "SIGNAL" else e
        for e in events
        if e.available_at >= at(25 * DAY)
    ]
    again = WalkForwardValidator(
        spec.model_copy(update={"snapshot_id": events_snapshot_id(corrupted)}), corrupted
    ).tune(factory)
    assert [a["train"] for a in tuned["attempts"]] == [a["train"] for a in again["attempts"]]
    assert [a["validation"] for a in tuned["attempts"]] == [
        a["validation"] for a in again["attempts"]
    ]
    result = validator.evaluate_holdout(tuned["selected"], factory)
    assert result["report"]["mode"] == "walk_forward_holdout"
    with pytest.raises(HoldoutError):
        validator.evaluate_holdout(tuned["selected"], factory)
    with pytest.raises(HoldoutError):
        validator.tune(factory)


def test_holdout_needs_tuning_first_and_reports_every_attempt_including_losers():
    events = world()
    validator = WalkForwardValidator(spec_for(events), events)
    with pytest.raises(ValueError, match="tuned"):
        validator.evaluate_holdout("small", factory)
    tuned = validator.tune(factory)
    assert {a["candidate_id"] for a in tuned["attempts"]} == {"small", "big"}
    assert all(a["train"]["net_pnl"] is not None for a in tuned["attempts"])


def test_snapshot_mismatch_is_rejected():
    events = world()
    spec = spec_for(events).model_copy(update={"snapshot_id": "snap-other"})
    with pytest.raises(ValueError, match="snapshot_id"):
        WalkForwardValidator(spec, events)


def test_multiple_testing_adjustment_and_sample_size_gate():
    events = world(n=45)
    validator = WalkForwardValidator(spec_for(events), events)
    tuned = validator.tune(factory)
    result = validator.evaluate_holdout(tuned["selected"], factory)
    sig = result["significance"]
    # Only ~20 independent holdout markets: significance must stay disabled.
    assert sig["status"] == "disabled" and "insufficient_independent_groups" in sig["reason"]
    assert sig["family_size"] == 2

    big = world(n=120)
    spec = ExperimentSpec(
        experiment_id="exp2",
        snapshot_id=events_snapshot_id(big),
        walk_forward=WalkForwardConfig(
            train_end=at(30 * DAY), validation_end=at(50 * DAY), end=at(125 * DAY)
        ),
        config=config(),
        candidates=[
            Candidate(candidate_id="a", params={"amount": "10"}),
            Candidate(candidate_id="b", params={"amount": "20"}),
        ],
        candidate_tokens=[f"tok-{i}" for i in range(120)],
    )
    v = WalkForwardValidator(spec, big)
    out = v.evaluate_holdout(v.tune(factory)["selected"], factory)["significance"]
    assert out["status"] == "computed" and out["independent_groups"] >= 30
    assert out["p_value_family_adjusted_bonferroni"] == min(
        1.0, out["p_value_one_sided_normal_approx"] * 2
    )
    assert "probability_of_backtest_overfitting" in out["not_computed"]


def test_baselines_and_independent_benchmark_report():
    events = world(n=6)
    cfg = config(decision_latency_ms=60_000)
    candidate = replay(events, follow, cfg, strategy_version="cand", mode="holdout")
    baselines = run_baselines(events, cfg, [f"tok-{i}" for i in range(6)], Decimal(50))
    assert set(baselines) == {"no_trade", "passive_holding", "naive_copy"}
    assert (
        baselines["no_trade"]["pnl"]["net_pnl"] in {"0", "0.0000", "0.00"}
        or Decimal(baselines["no_trade"]["pnl"]["net_pnl"]) == 0
    )
    assert baselines["no_trade"]["fills"] == []
    assert baselines["passive_holding"]["fills"]
    assert baselines["naive_copy"]["fills"]
    assert len({b["run"]["benchmark_id"] for b in baselines.values()}) == 1
    report = benchmark_report(candidate, baselines)
    assert report["same_data_snapshot"] is True
    assert set(report["net_pnl_minus_baseline"]) == set(baselines)
    assert report["comparison"]["claim"].startswith("modeled research comparison")


def test_compare_leaves_unknown_net_pnl_unranked():
    events = world(n=3)
    known = replay(events, follow, config(), strategy_version="a")
    unknown = replay(
        events,
        follow,
        config().model_copy(update={"taker_fee_bps": None, "fee_schedule_id": None}),
        strategy_version="b",
    )
    table = compare({"known": known, "unknown": unknown})
    assert table["ranking"] == ["known"] and table["unranked_unknown_net_pnl"] == ["unknown"]
    row = next(r for r in table["rows"] if r["name"] == "unknown")
    assert row["net_pnl"] is None and row["fee_status"] == "unknown"


def test_shadow_run_matches_paper_replay_and_never_signs():
    events = world(n=5)
    paper = replay(events, follow, config(), strategy_version="s")
    shadow = run_shadow(events, follow, config(), "s")
    assert parity(shadow, paper)["identical"] is True
    assert shadow["mode"] == "shadow_not_executed"
    assert shadow["shadow"] == {
        "orders_signed": 0,
        "orders_submitted_to_venue": 0,
        "note": shadow["shadow"]["note"],
    }


def test_shadow_session_is_incremental_and_deterministic_with_late_arrivals():
    events = world(n=3)
    session = ShadowSession(follow, config(), "s")
    for e in reversed(events):  # observation order differs from event order
        session.observe(e)
    assert (
        session.report()["result_hash"] == run_shadow(events, follow, config(), "s")["result_hash"]
    )
    bad = copy.deepcopy(events[0]).model_copy(update={"bids": []})
    with pytest.raises(ValueError, match="conflicting"):
        session.observe(bad)


def test_parity_flags_a_real_divergence():
    events = world(n=3)
    delayed = replay(events, follow, config(decision_latency_ms=5000), strategy_version="s")
    shadow = run_shadow(events, follow, config(), "s")
    result = parity(shadow, delayed)
    assert result["identical"] is False
    assert "orders" in result["differences"]
    assert result["same_data_snapshot"] is True
