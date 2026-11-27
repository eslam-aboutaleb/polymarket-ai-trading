import json
from types import SimpleNamespace

import pytest
from event_replay_fixtures import book, settlement, signal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import backtesting as legacy_routes
from app.api.routes.trading import strategy_lab as routes
from app.models.analytics.backtest_run import BacktestRun
from app.models.base import Base
from app.models.users.account import Account
from app.models.users.user import User
from app.utils.database import get_db

BASE = "/api/backtesting/lab"


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[User.__table__, Account.__table__, BacktestRun.__table__]
    )
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add_all(
            [User(id=1, wallet_address="0x" + "a" * 40), User(id=2, wallet_address="0x" + "b" * 40)]
        )
        db.flush()
        db.add_all(
            [
                Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True),
                Account(id=2, owner_user_id=1, name="Sub", kind="sub"),
                Account(id=3, owner_user_id=2, name="Other", kind="personal", is_default=True),
            ]
        )
        db.commit()

    def sessions():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(legacy_routes.router)
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(event_replay_lab_enabled=True)
    )
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(accounts_enabled=True))
    with TestClient(app) as http:
        yield http, factory, app
    engine.dispose()


def payload(**overrides):
    events = [
        book("b1", 0, [("0.48", 500)], [("0.50", 500)]),
        signal("s1", 1, source_pnl="5000"),
        settlement("f", 100, "1"),
    ]
    body = {
        "name": "api run",
        "events": [json.loads(e.model_dump_json()) for e in events],
        "config": {
            "funding": "1000",
            "taker_fee_bps": "100",
            "fee_schedule_id": "fee-v1",
            "seed": 3,
        },
        "strategy": {"name": "follow_signals", "follower_notional_usdc": "50"},
        "baseline_tokens": ["tok-yes"],
    }
    body.update(overrides)
    return body


def test_event_replay_persists_reports_baselines_and_is_reproducible(client):
    http, factory, _ = client
    created = http.post(f"{BASE}/event-replays", json=payload())
    assert created.status_code == 200, created.text
    body = created.json()
    assert "SIMULATED" in body["mode_watermark"]
    report = body["report"]
    assert report["result_class"] == "simulated_not_exchange_confirmed"
    assert report["conservation"]["holds"] is True
    assert set(body["baselines"]) == {"no_trade", "passive_holding", "naive_copy"}
    assert body["benchmark"]["same_data_snapshot"] is True
    # Leader P&L of 5000 never reaches the follower result.
    assert float(report["pnl"]["net_pnl"]) < 100
    with factory() as db:
        row = db.get(BacktestRun, body["id"])
        assert row.account_id == 1 and row.strategy_type == "event_replay"
    assert http.get(f"{BASE}/event-replays/{body['id']}").json()["report"] == report
    verified = http.post(f"{BASE}/event-replays/{body['id']}/verify")
    assert verified.json()["identical"] is True


def test_shadow_run_is_labeled_and_signs_nothing(client):
    http, _, _ = client
    result = http.post(f"{BASE}/shadow-runs", json=payload())
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["run_type"] == "shadow_run"
    assert "No order was sent or signed" in body["mode_watermark"]
    assert body["report"]["shadow"]["orders_signed"] == 0
    assert body["baselines"] is None


def test_account_and_user_isolation(client, monkeypatch):
    http, factory, _ = client

    def stored_runs(user_id, limit=20):
        with factory() as db:
            return db.query(BacktestRun).filter_by(user_id=user_id).limit(limit).all()

    monkeypatch.setattr(legacy_routes, "get_backtest_runs", stored_runs)
    run_id = http.post(f"{BASE}/event-replays", json=payload()).json()["id"]
    other = {"X-Account-Id": "2"}
    assert http.get(f"{BASE}/event-replays/{run_id}", headers=other).status_code == 404
    assert http.post(f"{BASE}/event-replays/{run_id}/verify", headers=other).status_code == 404
    assert http.get(f"{BASE}/runs", headers=other).json() == {"runs": []}
    assert http.get(f"{BASE}/runs").json()["runs"][0]["id"] == run_id
    assert http.get(f"{BASE}/runs", headers={"X-Account-Id": "3"}).status_code == 403
    # The legacy list must not leak account-scoped lab runs into another account.
    legacy = http.get("/api/backtesting/runs", headers=other).json()
    assert legacy["total"] == 0
    assert http.get("/api/backtesting/runs").json()["total"] == 1


def test_compare_ranks_known_and_leaves_unknown_unranked(client):
    http, _, _ = client
    known = http.post(f"{BASE}/event-replays", json=payload()).json()["id"]
    unknown_body = payload()
    unknown_body["config"] = {"funding": "1000", "seed": 3}
    unknown = http.post(f"{BASE}/event-replays", json=unknown_body).json()
    assert unknown["report"]["pnl"]["net_pnl"] is None
    assert unknown["net_pnl_quality"] == "unknown"
    table = http.get(f"{BASE}/strategy-lab/compare", params={"run_ids": [known, unknown["id"]]})
    assert table.status_code == 200
    data = table.json()
    assert len(data["ranking"]) == 1 and len(data["unranked_unknown_net_pnl"]) == 1
    assert (
        http.get(f"{BASE}/strategy-lab/compare", params={"run_ids": [known, 999]}).status_code
        == 404
    )
    assert http.get(f"{BASE}/strategy-lab/compare", params={"run_ids": [known]}).status_code == 422


def test_invalid_inputs_are_rejected_and_nothing_is_stored(client):
    http, factory, _ = client
    bad = payload()
    bad["events"][1]["event_time"] = "2027-01-01T00:00:00Z"
    assert http.post(f"{BASE}/event-replays", json=bad).status_code == 422
    conflicting = payload()
    conflicting["events"].append({**conflicting["events"][0], "bids": []})
    assert http.post(f"{BASE}/event-replays", json=conflicting).status_code == 409
    half_fee = payload()
    half_fee["config"] = {"funding": "1000", "taker_fee_bps": "100"}
    assert http.post(f"{BASE}/event-replays", json=half_fee).status_code == 422
    with factory() as db:
        assert db.query(BacktestRun).count() == 0


def test_auth_and_feature_flag(client, monkeypatch):
    http, _, app = client
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(event_replay_lab_enabled=False)
    )
    assert http.post(f"{BASE}/event-replays", json=payload()).status_code == 503
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(event_replay_lab_enabled=True)
    )
    app.dependency_overrides.pop(get_current_user_from_token)
    assert http.get(f"{BASE}/runs").status_code == 401
