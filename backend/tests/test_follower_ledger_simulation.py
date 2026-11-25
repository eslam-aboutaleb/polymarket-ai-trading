"""#58: follower simulation publishes net metrics only from a reconcilable ledger."""

import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from follower_fixtures import experiment_payload
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import follower_replay as routes
from app.models.analytics.follower_experiment import FollowerExperimentRun
from app.models.base import Base
from app.models.users.account import Account
from app.models.users.user import User
from app.services.analytics.replay.follower import FollowerExperiment, replay
from app.services.analytics.replay.reconciliation import reconcile
from app.utils.database import get_db


def run(payload=None):
    return replay(FollowerExperiment.model_validate(payload or experiment_payload()))


def test_every_scenario_reconciles_and_publishes_net_metrics():
    result = run()
    for scenario in result["scenarios"]:
        assert scenario["reconciliation"]["status"] == "reconciled"
        assert scenario["reconciliation"]["events"] == len(scenario["journal"]) > 0
        assert scenario["valuation"]["net_pnl"] is not None
        assert "metrics_status" not in scenario


def test_run_records_start_funding_sizing_fee_model_and_dataset_version():
    result = run()
    assert result["start_at"] == "2026-10-01T00:00:00+00:00"
    assert result["funding"] == "1000"
    assert result["sizing"]["mode"] == "fixed_notional"
    assert result["fee_model"] == {"fee_bps": "0"}
    assert result["dataset_version"]["hash"] == result["dataset_hash"]
    changed = experiment_payload()
    changed["dataset"]["books"][0]["asks"][0]["price"] = ".41"
    assert run(changed)["dataset_version"]["hash"] != result["dataset_version"]["hash"]


def test_tampered_journal_or_valuation_fails_reconciliation_and_withholds_metrics():
    scenario = run()["scenarios"][0]
    funding = Decimal("1000")
    forged = deepcopy(scenario["journal"])
    forged[1]["cash"] = "1000000"
    check = reconcile(funding, forged, scenario["valuation"])
    assert check["status"] == "mismatch" and check["problems"]
    valuation = {**scenario["valuation"], "fees": "1"}
    assert reconcile(funding, scenario["journal"], valuation)["status"] == "mismatch"
    positions = {**scenario["valuation"], "positions": {"YES": {"shares": "5", "cost_basis": "1"}}}
    assert reconcile(funding, scenario["journal"], positions)["status"] == "mismatch"
    assert (
        reconcile(Decimal("999"), scenario["journal"], scenario["valuation"])["status"]
        == "mismatch"
    )
    broken = [{**scenario["journal"][0], "kind": "NOPE"}]
    assert reconcile(funding, broken, scenario["valuation"])["status"] == "mismatch"
    orphan = [{**scenario["journal"][1], "kind": "FILL", "details": ["missing", "1", "0.5"]}]
    assert (
        "could not be replayed" in reconcile(funding, orphan, scenario["valuation"])["problems"][0]
    )


def test_fees_and_settlement_reconcile():
    payload = experiment_payload()
    payload["fee_bps"] = "200"
    payload["dataset"]["settlements"] = [
        {"event_id": "s", "token_id": "YES", "observed_at": "2026-10-01T00:30:00Z", "payout": "1"}
    ]
    payload["start_at"] = "2026-10-01T00:00:00Z"
    # entry only: drop the exit so the position is held to settlement
    payload["dataset"]["signals"] = payload["dataset"]["signals"][:1]
    for scenario in run(payload)["scenarios"]:
        assert scenario["reconciliation"]["status"] == "reconciled"
        assert Decimal(scenario["valuation"]["fees"]) > 0
        assert scenario["valuation"]["positions"] == {}


def test_different_follow_start_dates_give_different_histories():
    early = run()
    late_payload = experiment_payload()
    late_payload["start_at"] = "2026-10-01T00:05:00Z"
    late = run(late_payload)
    assert early["scenarios"][0]["valuation"]["net_pnl"] == "7.5"
    assert late["scenarios"][0]["valuation"]["net_pnl"] == "0"
    assert late["scenarios"][0]["journal"] == []
    assert early["start_at"] != late["start_at"]


def test_zero_volume_book_is_unfilled_unknown_not_profit():
    payload = experiment_payload()
    for book in payload["dataset"]["books"]:
        for level in book["asks"] + book["bids"]:
            level["shares"] = "0"
    for scenario in run(payload)["scenarios"]:
        assert all(o["status"] == "unfilled" for o in scenario["outcomes"])
        assert scenario["valuation"]["net_pnl"] == "0"
        assert scenario["reconciliation"]["status"] == "reconciled"


def test_delay_changes_fills_and_partial_fill_reconciles():
    fast, slow = run()["scenarios"]
    assert fast["outcomes"][0]["status"] == "partial"
    assert fast["valuation"]["net_pnl"] != slow["valuation"]["net_pnl"]
    assert slow["reconciliation"]["status"] == "reconciled"


def test_cash_fraction_sizing_scales_with_available_cash():
    payload = experiment_payload()
    payload["sizing"] = "cash_fraction"
    payload["cash_fraction"] = "0.01"
    payload["dataset"]["books"][0]["asks"][0]["shares"] = "1000"
    result = run(payload)
    entry = result["scenarios"][0]["outcomes"][0]
    assert Decimal(entry["notional"]) == Decimal("10.00")  # 1% of 1000
    payload["funding"] = "5000"
    assert Decimal(run(payload)["scenarios"][0]["outcomes"][0]["notional"]) == Decimal("50.00")
    assert result["sizing"]["mode"] == "cash_fraction"
    with pytest.raises(ValueError):
        FollowerExperiment.model_validate({**experiment_payload(), "sizing": "all_in"})


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[User.__table__, Account.__table__, FollowerExperimentRun.__table__]
    )
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(id=1, wallet_address="0x" + "a" * 40))
        db.flush()
        db.add(Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True))
        db.commit()

    def sessions():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(follower_replay_enabled=True)
    )
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(accounts_enabled=True))
    with TestClient(app) as http:
        yield http, factory
    engine.dispose()


def test_stored_run_that_was_edited_loses_net_metrics_on_read(client):
    http, factory = client
    created = http.post("/api/copy-trading/experiments", json=experiment_payload()).json()
    assert created["result"]["scenarios"][0]["valuation"]["net_pnl"] == "7.5"
    with factory() as db:
        row = db.get(FollowerExperimentRun, created["id"])
        stored = json.loads(row.result_json)
        stored["scenarios"][0]["journal"][-1]["cash"] = "999999"
        stored["scenarios"][0]["valuation"]["net_pnl"] = "999"
        row.result_json = json.dumps(stored)
        db.commit()
    read = http.get("/api/copy-trading/experiments/" + created["id"]).json()
    tampered, intact = read["result"]["scenarios"]
    assert tampered["reconciliation"]["status"] == "mismatch"
    assert tampered["valuation"]["net_pnl"] is None
    assert tampered["metrics_status"] == "withheld_unreconciled"
    assert intact["valuation"]["net_pnl"] == "-2.5"


def test_actions_distinguish_watch_simulate_and_disabled_live_follow(client, monkeypatch):
    http, _ = client
    body = http.get("/api/copy-trading/experiments/actions").json()
    assert body["watch"]["enabled"] and not body["watch"]["ledger"]
    assert body["simulate"]["enabled"] and body["simulate"]["ledger"]
    assert body["live_follow"]["enabled"] is False
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(follower_replay_enabled=False)
    )
    assert http.get("/api/copy-trading/experiments/actions").json()["simulate"]["enabled"] is False
