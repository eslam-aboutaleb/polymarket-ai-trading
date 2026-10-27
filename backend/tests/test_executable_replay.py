from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.analytics.replay.executable import replay
from app.services.analytics.replay.executable_contracts import ReplayDefinition
from app.services.analytics.replay.nav_statistics import family_statistics, periodic_statistics

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def quote(identity, seconds, bid="0.39", ask="0.40", depth="100"):
    return {
        "snapshot_id": identity,
        "token_id": "yes",
        "condition_id": "condition",
        "rule_hash": "rules",
        "received_at": NOW + timedelta(seconds=seconds),
        "source_at": NOW + timedelta(seconds=seconds),
        "connection_id": "connection",
        "kind": "snapshot",
        "bids": [{"price": bid, "shares": depth}],
        "asks": [{"price": ask, "shares": depth}],
    }


def signal(identity, seconds, side):
    return {
        "id": identity,
        "condition_id": "condition",
        "token_id": "yes",
        "side": side,
        "available_at": NOW + timedelta(seconds=seconds),
        "source_at": NOW + timedelta(seconds=seconds),
        "units": "SHARES",
        "amount": "10",
    }


def definition(**changes):
    data = {
        "start_at": NOW,
        "train_end": NOW + timedelta(seconds=2),
        "validation_end": NOW + timedelta(seconds=5),
        "end_at": NOW + timedelta(seconds=8),
        "sample_seconds": 1,
        "hypothesis": "Frozen follower units, no leader profits",
        "trials": [
            {
                "id": "base",
                "strategy_version": "fixture-v1",
                "fee_bps": "0",
                "settlement_fee_bps": "0",
                "fee_version": "explicit-fixture",
            }
        ],
        "tape": {
            "specs": [
                {
                    "condition_id": "condition",
                    "yes_token": "yes",
                    "no_token": "no",
                    "rule_hash": "rules",
                    "metadata_id": "meta",
                    "received_at": NOW,
                    "active": True,
                    "supported": True,
                    "tick": "0.01",
                    "minimum_shares": "1",
                }
            ],
            "events": [quote("first", 1), quote("close", 4, "0.45", "0.46")],
            "signals": [signal("entry", 0, "BUY"), signal("exit", 3, "SELL")],
        },
    }
    data.update(changes)
    return ReplayDefinition.model_validate(data)


def test_golden_cost_stress_reverses_gross_profit_and_replays_identically():
    body = definition()
    trials = [
        body.trials[0],
        body.trials[0].model_copy(update={"id": "stress", "fee_bps": Decimal(1000)}),
    ]
    body = body.model_copy(update={"trials": trials})
    result = replay(body)
    assert result == replay(ReplayDefinition.model_validate_json(body.model_dump_json()))
    gross, net = result["trial_results"]
    assert Decimal(gross["net_pnl"]) == Decimal("0.50")
    assert Decimal(net["net_pnl"]) == Decimal("-0.35")
    assert Decimal(net["gross_pnl_on_matched_quantity"]) == Decimal("0.50")
    assert Decimal(net["fees_usdc"]) == Decimal("0.85")
    assert result["actual_fills"] is None and not result["execution_eligible"]


def test_same_receipt_cannot_fill_and_positive_latency_requires_later_source():
    body = definition()
    event = quote("same", 0)
    body = body.model_copy(
        update={
            "tape": body.tape.model_copy(
                update={"events": [body.tape.events[0].model_validate(event)]}
            )
        }
    )
    result = replay(body)["trial_results"][0]
    assert result["trade_count"] == 0
    original = definition()
    trial = original.trials[0].model_copy(update={"decision_latency_ms": 2000})
    result = replay(original.model_copy(update={"trials": [trial]}))["trial_results"][0]
    assert result["outcomes"][0]["available_at"] == (NOW + timedelta(seconds=4)).isoformat()
    assert result["outcomes"][0]["snapshot_ids"] == ["first", "close"]


def test_partial_depth_cancels_remainder_and_never_forces_final_exit():
    body = definition()
    event = body.tape.events[0].model_validate(quote("partial", 1, depth="4"))
    body = body.model_copy(
        update={
            "tape": body.tape.model_copy(
                update={"events": [event], "signals": [body.tape.signals[0]]}
            )
        }
    )
    result = replay(body)["holdout"]
    assert result["outcomes"][0]["status"] == "partial"
    assert result["outcomes"][0]["remainder"] == "cancelled"
    assert result["trade_count"] == 1
    assert result["valuation"]["available_cash"] == result["valuation"]["cash"]
    assert Decimal(result["valuation"]["positions"]["yes"]["shares"]) == 4
    assert not any(row["kind"] == "SETTLED" for row in result["ledger"])


def test_sample_count_comes_from_grid_not_trade_count_and_missing_nav_abstains():
    points = [{"epoch": i * 86400, "nav": "1000" if i % 2 else "1001"} for i in range(31)]
    result = periodic_statistics(points, 365, 86400)
    assert result["return_samples"] == 30 and result["sharpe_ratio"] is not None
    points[10]["nav"] = None
    result = periodic_statistics(points, 365, 86400)
    assert result["sharpe_ratio"] is None and result["reason"] == "MISSING_OR_NONPERIODIC_NAV"
    assert replay(definition())["trial_results"][0]["statistics"]["sharpe_ratio"] is None


def test_future_holdout_quotes_cannot_choose_validation_winner():
    body = definition()
    before = replay(body)
    extra = body.tape.events[0].model_validate(quote("future", 7, "0.80", "0.90"))
    after = replay(
        body.model_copy(
            update={"tape": body.tape.model_copy(update={"events": [*body.tape.events, extra]})}
        )
    )
    assert before["trial_results"] == after["trial_results"]
    assert before["selected_trial_id"] == after["selected_trial_id"]


def test_family_metrics_are_unavailable_without_declared_attempts_and_samples():
    assert (
        family_statistics([[0.01, 0.02], [0.03, 0.01]], False)["deflated_sharpe_probability"]
        is None
    )
    assert family_statistics([[0.01, 0.02], [0.03, 0.01]], True)["pbo"] is None


def test_token_swap_rejected_before_economic_replay():
    body = definition().model_dump(mode="json")
    meta = dict(body["tape"]["specs"][0], metadata_id="swapped", yes_token="no", no_token="yes")
    body["tape"]["specs"].append(meta)
    with pytest.raises(ValueError, match="token-swap"):
        ReplayDefinition.model_validate(body)


def test_disputed_then_explicit_void_settlement_reconciles_cash_and_cost_basis():
    body = definition(
        train_end=NOW + timedelta(seconds=4), validation_end=NOW + timedelta(seconds=6)
    )
    body = body.model_copy(
        update={"tape": body.tape.model_copy(update={"signals": [body.tape.signals[0]]})}
    )
    payload = body.model_dump(mode="json")
    base = {
        "condition_id": "condition",
        "token_id": "yes",
        "rule_hash": "rules",
        "source_ids": ["settlement-proof"],
    }
    payload["tape"]["settlements"] = [
        {
            **base,
            "id": "dispute",
            "status": "DISPUTED",
            "available_at": NOW + timedelta(seconds=2),
            "source_at": NOW + timedelta(seconds=2),
        },
        {
            **base,
            "id": "void",
            "status": "VOID",
            "confirmed": True,
            "payout": "0.5",
            "available_at": NOW + timedelta(seconds=4),
            "source_at": NOW + timedelta(seconds=4),
        },
    ]
    result = replay(ReplayDefinition.model_validate(payload))["holdout"]
    assert result["nav"][2]["nav"] is None
    assert Decimal(result["valuation"]["cash"]) == 1001
    assert not result["valuation"]["positions"]
    assert Decimal(result["valuation"]["realized_pnl"]) == 1


def test_expiry_missing_depth_and_resolution_delay_never_force_exit():
    body = definition()
    raw = body.model_dump(mode="json")
    raw["tape"]["signals"] = [signal("entry", 0, "BUY"), signal("late", 8, "SELL")]
    raw["tape"]["settlements"] = [
        {
            "id": "expiry",
            "condition_id": "condition",
            "token_id": "yes",
            "rule_hash": "rules",
            "available_at": NOW + timedelta(seconds=6),
            "source_at": NOW + timedelta(seconds=6),
            "status": "EXPIRED",
            "source_ids": ["pending-resolution"],
        }
    ]
    result = replay(ReplayDefinition.model_validate(raw))["holdout"]
    assert result["net_pnl"] is None
    assert result["trade_count"] == 1
    assert result["outcomes"][-1]["status"] == "unfilled"
    assert result["valuation"]["cash"] == "996.00"
    assert result["statistics"]["sharpe_ratio"] is None
    assert result["splits"]["test"]["reason"] == "MISSING_OR_NONPERIODIC_NAV"


def test_adverse_slippage_and_depth_budget_are_explicit():
    body = definition()
    raw = body.model_dump(mode="json")
    raw["trials"][0]["slippage_bps"] = "1000"
    result = replay(ReplayDefinition.model_validate(raw))["holdout"]
    assert Decimal(result["net_pnl"]) < 0
    assert Decimal(result["slippage_usdc"]) > 0
    raw["trials"][0]["slippage_bps"] = "0"
    raw["tape"]["events"] = [quote("one", 1, depth="10"), quote("repeat", 4, depth="10")]
    raw["tape"]["signals"] = [signal("first", 0, "BUY"), signal("again", 3, "BUY")]
    result = replay(ReplayDefinition.model_validate(raw))["holdout"]
    assert result["trade_count"] == 1
    assert result["outcomes"][1]["status"] == "unfilled"


def test_final_conflict_freezes_valuation_after_payout_and_settlement_fee_reconciles():
    raw = definition().model_dump(mode="json")
    raw["tape"]["signals"] = [signal("entry", 0, "BUY")]
    raw["trials"][0]["settlement_fee_bps"] = "1000"
    raw["tape"]["settlements"] = [
        {
            "id": "final",
            "condition_id": "condition",
            "token_id": "yes",
            "rule_hash": "rules",
            "available_at": NOW + timedelta(seconds=4),
            "source_at": NOW + timedelta(seconds=4),
            "status": "FINAL",
            "payout": "1",
            "confirmed": True,
            "source_ids": ["resolution"],
        }
    ]
    first = replay(ReplayDefinition.model_validate(raw))["holdout"]
    assert first["net_pnl"] == "5.00"
    assert Decimal(first["fees_usdc"]) == Decimal("1")
    raw["tape"]["settlements"].append(
        {
            **raw["tape"]["settlements"][0],
            "id": "conflict",
            "available_at": NOW + timedelta(seconds=7),
            "source_at": NOW + timedelta(seconds=7),
            "payout": "0",
        }
    )
    result = replay(ReplayDefinition.model_validate(raw))["holdout"]
    assert result["net_pnl"] is None
    assert result["valuation"]["reconciliation_required"]
    assert result["valuation"]["cash"] == "1005.00"


def test_periodic_sharpe_and_family_diagnostics_require_and_use_aligned_nav():
    import math

    series = [
        [0.002 * math.sin(i * (trial + 1) + trial) + 0.0001 * trial for i in range(80)]
        for trial in range(8)
    ]
    nav, points = 1000.0, [{"epoch": 0, "nav": "1000"}]
    for i, value in enumerate(series[0]):
        nav *= 1 + value
        points.append({"epoch": (i + 1) * 86400, "nav": str(nav)})
    stats = periodic_statistics(points, 365, 86400)
    assert stats["return_samples"] == 80
    assert stats["sharpe_ratio"] is not None
    assert len(stats["mean_return_ci95"]) == 2
    family = family_statistics(series, True)
    assert 0 <= family["pbo"] <= 1
    assert 0 <= family["deflated_sharpe_probability"] <= 1
    assert family["cscv_partitions"] == 70


def test_later_rule_change_cannot_be_cleared_by_old_rule_payout():
    raw = definition().model_dump(mode="json")
    raw["tape"]["signals"] = [signal("entry", 0, "BUY")]
    raw["tape"]["specs"].append(
        {
            **raw["tape"]["specs"][0],
            "metadata_id": "changed",
            "rule_hash": "changed-rules",
            "received_at": NOW + timedelta(seconds=6),
        }
    )
    raw["tape"]["settlements"] = [
        {
            "id": "old-final",
            "condition_id": "condition",
            "token_id": "yes",
            "rule_hash": "rules",
            "available_at": NOW + timedelta(seconds=7),
            "source_at": NOW + timedelta(seconds=7),
            "status": "FINAL",
            "payout": "1",
            "confirmed": True,
            "source_ids": ["old-resolution"],
        }
    ]
    selected = replay(ReplayDefinition.model_validate(raw))["holdout"]
    assert selected["net_pnl"] is None
    assert selected["valuation"]["cash"] == "996.00"
    assert selected["valuation"]["reconciliation_required"]


def test_duplicate_series_cannot_inflate_effective_family_trials():
    values = [i % 3 * 0.001 for i in range(80)]
    result = family_statistics([values] * 8, True)
    assert result["pbo"] is None and result["deflated_sharpe_probability"] is None
    assert result["reason"] == "DUPLICATE_RETURN_SERIES_NOT_DISTINCT_TRIALS"
