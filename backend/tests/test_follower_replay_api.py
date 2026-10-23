import json
from types import SimpleNamespace
from unittest.mock import patch

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
from app.utils.database import get_db


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
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(follower_replay_enabled=True)
    )
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(accounts_enabled=True))
    with TestClient(app) as http:
        yield http, factory, app
    engine.dispose()


def test_real_api_persistence_new_session_and_account_isolation(client):
    http, factory, _ = client
    with patch(
        "py_clob_client.client.ClobClient.post_order",
        side_effect=AssertionError("paper cannot post"),
    ):
        result = http.post("/api/copy-trading/experiments", json=experiment_payload())
    assert result.status_code == 200, result.text
    body = result.json()
    run_id = body["id"]
    assert body["result"]["scenarios"][0]["valuation"]["net_pnl"] == "7.5"
    with factory() as db:
        row = db.get(FollowerExperimentRun, run_id)
        assert json.loads(row.input_json)["wallet"] == experiment_payload()["wallet"]
    assert http.get("/api/copy-trading/experiments/" + run_id).json() == body
    assert (
        http.get(
            "/api/copy-trading/experiments/" + run_id, headers={"X-Account-Id": "2"}
        ).status_code
        == 404
    )
    assert http.get("/api/copy-trading/experiments", headers={"X-Account-Id": "2"}).json() == {
        "runs": []
    }
    assert (
        http.get("/api/copy-trading/experiments", headers={"X-Account-Id": "3"}).status_code == 403
    )
    assert http.get("/api/copy-trading/experiments?limit=101").status_code == 422


def test_auth_and_rollout_flag(client, monkeypatch):
    http, _, app = client
    app.dependency_overrides.pop(get_current_user_from_token)
    assert http.get("/api/copy-trading/experiments").status_code == 401
    monkeypatch.setattr(
        routes, "get_settings", lambda: SimpleNamespace(follower_replay_enabled=False)
    )
    assert http.post("/api/copy-trading/experiments", json=experiment_payload()).status_code == 503


def test_invalid_input_or_persistence_failure_never_returns_completed_run(client, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    http, factory, _ = client
    payload = experiment_payload()
    payload["leader_pnl"] = 100000
    assert http.post("/api/copy-trading/experiments", json=payload).status_code == 422
    with monkeypatch.context() as mocked:
        mocked.setattr(
            factory.class_,
            "commit",
            lambda self: (_ for _ in ()).throw(SQLAlchemyError("private storage error")),
        )
        result = http.post("/api/copy-trading/experiments", json=experiment_payload())
        assert result.status_code == 503 and "private" not in result.text
    assert http.get("/api/copy-trading/experiments").json() == {"runs": []}
