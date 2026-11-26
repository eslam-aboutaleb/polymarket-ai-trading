from datetime import timedelta
from decimal import Decimal

import pytest
from book_research_fixtures import CID, NOW
from book_research_fixtures import research_db as _research_db
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.analysis import opportunities as route
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models import Assessment
from app.utils.database import get_db

research_db = _research_db
CLOCK = NOW + timedelta(seconds=5)


@pytest.fixture
def client(research_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "executable_opportunities_enabled", True)
    monkeypatch.setattr(route, "utc_now", lambda: CLOCK)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_db] = lambda: research_db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as c:
        yield c


def add_assessment(db, probability, age_seconds=60):
    db.add(
        Assessment(
            ai_score=50,
            reasoning="r",
            recommendation="copy",
            risk_level="low",
            confidence=50,
            ai_probability=probability,
            condition_id=CID,
            created_at=CLOCK - timedelta(seconds=age_seconds),
        )
    )
    db.commit()


def test_disabled_by_default(research_db):
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_db] = lambda: research_db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as c:
        assert c.get("/api/opportunities").status_code == 503


def test_stored_forecasts_rank_as_indicative_only_and_never_guaranteed(client, research_db):
    add_assessment(research_db, 0.80)
    data = client.get(
        "/api/opportunities", params={"shares": "5", "fee_bps": "100", "fee_version": "t"}
    ).json()
    assert data["guaranteed_profit"] is False and data["mode"] == "research_only_no_order"
    assert [o["outcome"] for o in data["ranked"]] == ["YES"]
    top = data["ranked"][0]
    # Stored verdicts have no interval, calibration or rule hash, so they cannot be VERIFIED.
    assert top["status"] == "INDICATIVE"
    assert {"NO_UNCERTAINTY_INTERVAL", "FORECAST_NOT_CALIBRATED", "RULES_NOT_VERIFIED"} <= set(
        top["reasons"]
    )
    assert Decimal(top["executable_vwap"]) == Decimal("0.5")
    # The NO side has no edge for a high YES probability and is filtered with its reason.
    assert any(f["opportunity"]["outcome"] == "NO" for f in data["filtered"])


def test_stale_forecasts_and_missing_books_drop_out_and_min_edge_filters(client, research_db):
    add_assessment(research_db, 0.80, age_seconds=7200)
    assert client.get("/api/opportunities", params={"max_age": "3600"}).json()["ranked"] == []
    add_assessment(research_db, 0.80)
    cut = client.get(
        "/api/opportunities",
        params={"shares": "5", "fee_bps": "100", "fee_version": "t", "min_edge": "5"},
    ).json()
    assert cut["ranked"] == [] and cut["filtered"][0]["reason"] in {
        "BELOW_MIN_EDGE",
        "NO_NET_EDGE_AFTER_EXECUTABLE_COST",
    }
    missing_fee = client.get("/api/opportunities", params={"shares": "5"}).json()
    assert missing_fee["ranked"] == []
    assert client.get("/api/opportunities", params={"strategy": "arbitrage"}).json()["ranked"] == []


def forecast_body(**changes):
    return {
        "forecast": {
            "forecast_id": "f1",
            "condition_id": CID,
            "yes_probability": "0.70",
            "interval_low": "0.65",
            "interval_high": "0.75",
            "forecast_at": (CLOCK - timedelta(minutes=5)).isoformat(),
            "calibrated": True,
            **changes,
        },
        "shares": "5",
        "fee_bps": "100",
        "fee_version": "t",
    }


def test_evaluate_endpoint_verifies_with_interval_calibration_and_matching_rules(client):
    from app.services.market_data.book_research import ResearchQuery, load_analysis

    spec_rule = load_analysis(
        client.app.dependency_overrides[get_db](), ResearchQuery(condition_id=CID, as_of=CLOCK)
    )[0]["rule_hash"]
    ok = client.post("/api/opportunities/evaluate", json=forecast_body(rule_hash=spec_rule)).json()
    yes = [o for o in ok["ranked"] if o["outcome"] == "YES"][0]
    assert yes["status"] == "VERIFIED" and yes["reasons"] == []
    assert Decimal(yes["net_ev"]) > 0 and yes["guaranteed_profit"] is False
    wrong = client.post(
        "/api/opportunities/evaluate", json=forecast_body(rule_hash="0" * 64)
    ).json()
    assert wrong["ranked"] == []
    assert {f["reason"] for f in wrong["filtered"]} == {"RULE_HASH_MISMATCH"}
    abstained = client.post(
        "/api/opportunities/evaluate", json=forecast_body(abstained=True)
    ).json()
    assert abstained["ranked"] == []
    assert client.post("/api/opportunities/evaluate", json={}).status_code == 422
