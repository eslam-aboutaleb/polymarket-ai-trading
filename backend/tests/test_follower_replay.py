from copy import deepcopy
from decimal import Decimal

import pytest
from follower_fixtures import experiment_payload
from pydantic import ValidationError

from app.services.analytics.replay.follower import FollowerExperiment, replay


def run(payload=None):
    return replay(FollowerExperiment.model_validate(payload or experiment_payload()))


def test_delay_changes_follower_pnl_and_golden_ledger():
    result = run()
    fast, slow = result["scenarios"]
    assert Decimal(fast["valuation"]["net_pnl"]) == Decimal("7.5")
    assert Decimal(slow["valuation"]["net_pnl"]) == Decimal("-2.5")
    assert not result["execution_eligible"]
    assert result["quality"] == "user_supplied_unverified"
    assert fast["valuation"]["positions"] == {}
    assert fast["outcomes"][0]["status"] == "partial"
    assert fast["outcomes"][0]["shares"] == "25"
    assert result == run()


def test_follow_start_excludes_entry_exit_cannot_short():
    payload = experiment_payload()
    payload["start_at"] = "2026-10-01T00:05:00Z"
    result = run(payload)
    for scenario in result["scenarios"]:
        assert len(scenario["outcomes"]) == 1
        assert scenario["outcomes"][0]["status"] == "unfilled"
        assert Decimal(scenario["valuation"]["net_pnl"]) == 0
        assert scenario["journal"] == []


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("books", "missing_eligible_book"),
        ("fee", "missing_fee_policy"),
        ("depth", "missing_depth"),
        ("stale", "stale_book"),
    ],
)
def test_missing_inputs_never_fabricate_fill(fault, reason):
    payload = experiment_payload()
    if fault == "books":
        payload["dataset"]["books"] = []
    elif fault == "fee":
        payload["fee_bps"] = None
    elif fault == "depth":
        for book in payload["dataset"]["books"]:
            book["asks"] = book["bids"] = []
    else:
        for book in payload["dataset"]["books"]:
            book["source_at"] = "2026-09-30T23:00:00Z"
    result = run(payload)
    for scenario in result["scenarios"]:
        assert all(
            row["status"] == "unfilled" and row["reason"] == reason for row in scenario["outcomes"]
        )
        assert scenario["journal"] == []


def test_fee_stress_reverses_gross_profit():
    payload = experiment_payload()
    payload["fee_bps"] = "4000"
    scenario = run(payload)["scenarios"][0]
    assert Decimal(scenario["valuation"]["net_pnl"]) < 0
    assert Decimal(scenario["valuation"]["fees"]) == 11


def test_duplicate_notifications_book_capacity_and_identity_conflicts():
    payload = experiment_payload()
    payload["dataset"]["signals"].append(deepcopy(payload["dataset"]["signals"][0]))
    assert run(payload)["scenarios"] == run()["scenarios"]


def test_open_position_missing_end_mark_is_unknown_not_profit():
    payload = experiment_payload()
    payload["dataset"]["signals"] = payload["dataset"]["signals"][:1]
    result = run(payload)
    assert result["scenarios"][0]["valuation"]["nav"] is None
    assert result["scenarios"][0]["valuation"]["missing_marks"] == ["YES"]


def test_settlement_and_no_lookahead_before_observation():
    payload = experiment_payload()
    payload["dataset"]["signals"] = payload["dataset"]["signals"][:1]
    payload["dataset"]["settlements"] = [
        {
            "event_id": "final",
            "token_id": "YES",
            "observed_at": "2026-10-01T00:30:00Z",
            "payout": "1",
        }
    ]
    assert Decimal(run(payload)["scenarios"][0]["valuation"]["net_pnl"]) == 15
    payload["dataset"]["settlements"][0]["observed_at"] = "2026-10-02T00:30:00Z"
    assert run(payload)["scenarios"][0]["valuation"]["nav"] is None


@pytest.mark.parametrize("change", ["conflict", "clock", "extra", "nan", "window"])
def test_invalid_contract_rejected(change):
    payload = experiment_payload()
    if change == "conflict":
        row = deepcopy(payload["dataset"]["signals"][0])
        row["side"] = "SELL"
        payload["dataset"]["signals"].append(row)
    elif change == "clock":
        payload["dataset"]["books"][0]["source_at"] = "2026-10-02T00:00:00Z"
    elif change == "extra":
        payload["leader_pnl"] = 999999
    elif change == "nan":
        payload["funding"] = "NaN"
    else:
        payload["start_at"] = payload["end_at"]
    with pytest.raises(ValidationError):
        run(payload)


def test_capacity_cannot_be_reused_by_multiple_signals_on_same_book():
    payload = experiment_payload()
    second = deepcopy(payload["dataset"]["signals"][0])
    second["event_id"] = "entry-second"
    payload["dataset"]["signals"].append(second)
    result = run(payload)
    fast = result["scenarios"][0]
    buys = [row for row in fast["outcomes"] if row["side"] == "BUY"]
    assert sum(Decimal(row["shares"]) for row in buys) == 25
    assert any(row["status"] == "unfilled" for row in buys)


def test_invalid_tick_cannot_produce_partial_booking():
    payload = experiment_payload()
    payload["dataset"]["books"][0]["asks"].append({"price": ".405", "shares": "100"})
    with pytest.raises(ValidationError, match="tick size"):
        run(payload)
