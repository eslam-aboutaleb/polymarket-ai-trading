from datetime import timedelta
from decimal import Decimal

import pytest
from book_research_fixtures import CID, NOW
from book_research_fixtures import research_db as _research_db
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import execution_estimate as route
from app.config import get_settings
from app.models import LiveOrder, UserTrade
from app.services.market_data.venue_observations import append_event
from app.utils.database import get_db

research_db = _research_db
CLOCK = {"now": NOW + timedelta(seconds=5)}


@pytest.fixture
def client(research_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "execution_estimator_enabled", True)
    monkeypatch.setattr(route, "utc_now", lambda: CLOCK["now"])
    CLOCK["now"] = NOW + timedelta(seconds=5)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_db] = lambda: research_db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as test_client:
        yield test_client


def body(**request):
    return {
        "condition_id": CID,
        "outcome": "YES",
        "request": {
            "order_intent_id": "intent-1",
            "side": "BUY",
            "shares": "20",
            "limit_price": "0.60",
            "fee_bps": "100",
            "fee_version": "flat-test",
            **request,
        },
    }


def test_disabled_by_default_and_requires_valid_input(research_db, monkeypatch):
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_db] = lambda: research_db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as c:
        assert c.post("/api/execution/estimate", json=body()).status_code == 503
        assert c.get("/api/execution/quality").status_code == 503


def test_estimate_walks_captured_book_and_never_exposes_order_action(client):
    response = client.post("/api/execution/estimate", json=body())
    assert response.status_code == 200, response.text
    data = response.json()
    est = data["estimate"]
    assert data["status"] == "modeled" and est["fill_status"] == "FULL"
    assert est["executable_vwap"] == "0.55" and est["live_order_action"] is False
    assert Decimal(est["best_ask"]) == Decimal("0.5") and est["fill_probability"] is None
    # Size rules reject instead of rounding; insufficient depth is not a full fill.
    assert (
        client.post("/api/execution/estimate", json=body(limit_price="0.555")).json()["reason"]
        == "REJECT_OFF_TICK_PRICE"
    )
    deep = client.post("/api/execution/estimate", json=body(shares="40")).json()["estimate"]
    assert deep["fill_status"] == "PARTIAL" and deep["fillable_quantity"] == "30"
    fok = client.post("/api/execution/estimate", json=body(shares="40", mode="FOK")).json()
    assert fok["estimate"]["executable_vwap"] is None
    CLOCK["now"] = NOW + timedelta(seconds=500)
    old = client.post("/api/execution/estimate", json=body()).json()
    assert old["status"] == "abstain" and old["estimate"]["executable_vwap"] is None


def test_quote_change_invalidates_prior_estimate_via_api(client, research_db):
    first = client.post("/api/execution/estimate", json=body()).json()
    record_id = first["record_id"]
    assert client.get(f"/api/execution/estimates/{record_id}/revalidate").json()["valid"]
    append_event(
        research_db,
        {
            "event_type": "price_change",
            "asset_id": "11",
            "market": CID,
            "timestamp": str(int((NOW + timedelta(seconds=6)).timestamp() * 1000)),
            "price_changes": [{"asset_id": "11", "price": "0.50", "size": "0", "side": "SELL"}],
        },
        received_at=NOW + timedelta(seconds=6),
        connection_id="native-rest",
    )
    research_db.commit()
    CLOCK["now"] = NOW + timedelta(seconds=7)
    after = client.get(f"/api/execution/estimates/{record_id}/revalidate").json()
    # An unsequenced delta cannot be applied: the book is unusable, so the estimate is void.
    assert after["valid"] is False and "CURRENT_BOOK_NOT_USABLE" in after["reasons"]
    again = client.post(
        "/api/execution/estimate", json={**body(), "prior_estimate_record_id": record_id}
    ).json()
    assert again["prior"]["valid"] is False


def test_estimates_are_account_scoped(client):
    record_id = client.post("/api/execution/estimate", json=body()).json()["record_id"]
    assert client.get(f"/api/execution/estimates/{record_id}/revalidate").status_code == 200
    other = client.get(
        f"/api/execution/estimates/{record_id}/revalidate", headers={"X-Account-Id": "2"}
    )
    assert other.status_code == 404
    assert (
        client.get(
            f"/api/execution/estimates/{record_id}/revalidate", headers={"X-Account-Id": "3"}
        ).status_code
        == 403
    )


def add_order(db, *, shares="20", matched="20", notional="11", settled=True, token="11", fee=None):
    trade = UserTrade(
        user_id=1,
        account_id=1,
        market_id=CID,
        token_id=token,
        action="buy",
        amount=float(notional),
        price=0.55,
        status="executed",
        fee_paid=fee,
    )
    db.add(trade)
    db.flush()
    order = LiveOrder(
        user_id=1,
        account_id=1,
        trade_id=trade.id,
        idempotency_key=f"k{trade.id}",
        fingerprint="f" * 64,
        strategy="manual",
        token_id=token,
        side="BUY",
        price=Decimal("0.60"),
        shares=Decimal(shares),
        notional=Decimal("12"),
        reserved=Decimal("12"),
        state="FILLED",
        order_hash=f"0x{trade.id}",
        response={"settled": settled, "size_matched": matched, "filled_notional": notional},
        created_at=CLOCK["now"] + timedelta(seconds=1),
    )
    db.add(order)
    db.commit()
    return order


def test_quality_matches_settled_fill_to_estimate_against_same_benchmark(client, research_db):
    assert client.get("/api/execution/quality").json()["summary"]["matched_settled"] == 0
    client.post("/api/execution/estimate", json=body())
    add_order(research_db, matched="20", notional="11.4")  # filled at 0.57 vs 0.55 estimate
    data = client.get("/api/execution/quality").json()
    assert data["summary"]["matched_settled"] == 1 and data["summary"]["unmatched_estimates"] == 0
    row = data["rows"][0]
    assert row["gap"]["status"] == "compared" and Decimal(row["gap"]["error_bps"]) > 0
    assert row["waterfall"]["fee"] is None and "fee_cost" in row["waterfall"]["components_missing"]
    assert Decimal(row["actual_vwap"]) == Decimal("0.57")
    assert data["summary"]["p95_error_bps"] is None  # one sample is not a distribution
    assert data["profitability_claim"] is False


def test_quality_excludes_unsettled_ambiguous_and_foreign_matches(client, research_db):
    client.post("/api/execution/estimate", json=body())
    add_order(research_db, settled=False)
    s = client.get("/api/execution/quality").json()["summary"]
    assert s["matched_settled"] == 0 and s["matched_but_not_settled"] == 1
    add_order(research_db)  # second candidate order: the pairing is now ambiguous
    s = client.get("/api/execution/quality").json()["summary"]
    assert s["ambiguous_estimates"] == 1 and s["matched_settled"] == 0
    other = client.get("/api/execution/quality", headers={"X-Account-Id": "2"}).json()
    assert other["summary"]["estimates_modeled"] == 0
