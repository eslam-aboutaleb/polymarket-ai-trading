# ruff: noqa: C408
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.allocation import router
from app.config import get_settings
from app.models.base import Base
from app.models.users.account import Account
from app.models.users.user import User
from app.utils.database import get_db

C1 = "0x" + "1" * 64
BODY = {
    "as_of": "2026-11-01T00:00:00Z",
    "account_cash": "1000",
    "max_worst_case_loss": "400",
    "candidates": [
        {
            "candidate_id": "a",
            "condition_id": C1,
            "token_id": "t",
            "outcome": "YES",
            "ask_price": "0.5",
            "depth_shares": "1000",
            "p_calibrated": "0.7",
            "calibration_oos_n": 100,
        }
    ],
}


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                User(id=1, wallet_address="0x" + "11" * 20),
                User(id=2, wallet_address="0x" + "22" * 20),
            ]
        )
        db.flush()
        db.add_all(
            [
                Account(id=1, owner_user_id=1, name="P", kind="personal", is_default=True),
                Account(id=2, owner_user_id=1, name="S", kind="sub", is_default=False),
                Account(id=3, owner_user_id=2, name="F", kind="personal", is_default=True),
            ]
        )
        db.commit()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
        monkeypatch.setattr(get_settings(), "portfolio_allocation_enabled", True)
        with TestClient(app) as c:
            yield c


def test_simulate_is_advisory_and_persists_account_scoped(client):
    r = client.post("/api/v1/portfolio/allocation/simulate?persist=true", json=BODY)
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["execution_eligible"] is False
    assert r.json()["scenario_id"]
    assert len(client.get("/api/v1/portfolio/scenarios").json()) == 1
    assert client.get("/api/v1/portfolio/scenarios", headers={"X-Account-Id": "2"}).json() == []
    assert (
        client.get("/api/v1/portfolio/scenarios", headers={"X-Account-Id": "3"}).status_code == 403
    )


def test_flag_off_and_validation(client, monkeypatch):
    bad = {**BODY, "kelly_cap": "0.9"}
    assert client.post("/api/v1/portfolio/allocation/simulate", json=bad).status_code == 422
    monkeypatch.setattr(get_settings(), "portfolio_allocation_enabled", False)
    assert client.post("/api/v1/portfolio/allocation/simulate", json=BODY).status_code == 503
