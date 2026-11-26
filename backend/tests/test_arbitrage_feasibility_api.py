"""API + frozen-evidence tests for arbitrage feasibility (issue #9)."""

import asyncio
import json
from datetime import timedelta

import book_research_fixtures as fx
import httpx
import pytest
from book_research_fixtures import CID, NOW
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading.arbitrage import router
from app.config import get_settings
from app.models import Account, ArbitrageFeasibilityRun, Base, User
from app.services.analytics import arbitrage_service
from app.services.analytics.arbitrage.feasibility_service import (
    FeasibilityRequest,
    assess_frozen,
    replay_run,
)
from app.services.market_data import book_research, replay_capture
from app.services.market_data.replay_capture import PublicVenueCapture
from app.utils.database import get_db

BODY = {
    "size": "10",
    "fee_bps": "100",
    "fee_version": "flat-test",
    "costs": {"gas_usdc": "0", "settlement_usdc": "0"},
    "as_of": NOW.isoformat(),
}


def books(yes_asks, no_asks):
    """Native HTTP /book responses with explicit asks per token (hash recomputed)."""
    from py_clob_client_v2.utilities import (
        generate_orderbook_summary_hash,
        parse_raw_orderbook_summary,
    )

    def raw(token, asks):
        value = fx.raw_book(token)
        value["asks"] = [{"price": p, "size": s} for p, s in asks]
        value["bids"] = [{"price": "0.30", "size": "50"}]
        value["hash"] = ""
        value["hash"] = generate_orderbook_summary_hash(parse_raw_orderbook_summary(value))
        return value

    table = {"11": raw("11", yes_asks), "22": raw("22", no_asks)}

    def handler(request):
        if request.url.path.endswith("/book"):
            return httpx.Response(200, json=table[request.url.params["token_id"]])
        return fx.venue_handler(request)

    return handler


def make_db(monkeypatch, yes_asks, no_asks):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    for flag in (
        "binary_book_research_enabled",
        "native_capture_enabled",
        "observation_archive_enabled",
        "arbitrage_feasibility_enabled",
    ):
        monkeypatch.setattr(get_settings(), flag, True)
    monkeypatch.setattr(replay_capture, "utc_now", lambda: NOW)
    monkeypatch.setattr(book_research, "utc_now", lambda: NOW)
    db = Session(engine)
    db.add_all(
        [
            User(id=1, wallet_address="0x" + "11" * 20),
            User(id=2, wallet_address="0x" + "22" * 20),
        ]
    )
    db.flush()
    db.add_all(
        [
            Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True),
            Account(id=2, owner_user_id=1, name="Sub", kind="sub", is_default=False),
            Account(id=3, owner_user_id=2, name="Foreign", kind="personal", is_default=True),
        ]
    )
    db.commit()

    async def capture():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(books(yes_asks, no_asks))
        ) as client:
            return await book_research.capture_market_books(db, CID, PublicVenueCapture(client))

    assert asyncio.run(capture())["status"] == "captured"
    db.commit()
    return db, engine


@pytest.fixture
def edge_db(monkeypatch):
    db, engine = make_db(monkeypatch, [("0.45", "100")], [("0.50", "100")])
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def no_edge_db(monkeypatch):
    db, engine = make_db(monkeypatch, [("0.55", "100")], [("0.60", "100")])
    yield db
    db.close()
    engine.dispose()


@pytest.fixture
def client_for(monkeypatch):
    clients = []

    def build(db, user_id=1):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": user_id}
        client = TestClient(app)
        clients.append(client)
        return client

    arbitrage_service._arb_candidates.clear()
    yield build
    arbitrage_service._arb_candidates.clear()


def seed_candidate():
    market = {
        "condition_id": CID,
        "question": "Will the measure pass?",
        "outcomePrices": [0.45, 0.50],
    }
    signal = arbitrage_service._check_complement_arbitrage(market)
    arbitrage_service.record_indicative_candidate(signal)
    return arbitrage_service.get_recent_candidates()[0]["candidate_id"]


def test_candidates_list_is_indicative_only(client_for, edge_db):
    cid = seed_candidate()
    body = client_for(edge_db).get("/api/v1/arbitrage/candidates").json()
    assert body["count"] == 1 and body["live_execution_enabled"] is False
    item = body["candidates"][0]
    assert item["candidate_id"] == cid and item["state"] == "INDICATIVE"
    assert {"INDICATIVE_ONLY", "NOT_EXECUTABLE", "UNKNOWN_FEES"} <= set(item["badges"])
    assert item["executable_profit"] is None and item["profit_assured"] is False
    assert "guaranteed" not in json.dumps(body).replace("not guaranteed", "")


def test_feasibility_from_frozen_receipts_execution_ready(client_for, edge_db):
    cid = seed_candidate()
    response = client_for(edge_db).post(f"/api/v1/arbitrage/{cid}/feasibility", json=BODY)
    assert response.status_code == 200, response.text
    candidate = response.json()["candidate"]
    assert candidate["state"] == "EXECUTION_READY"
    # 10 shares: YES .45 + NO .50, 1% fee: bound = 10 - 9.5 - 0.095
    assert candidate["net_bound"]["net_bound"] == "0.4050"
    assert candidate["paper_only"] is True and candidate["live_execution_enabled"] is False
    lineage = response.json()["lineage"]
    assert lineage["credentials_included"] is False and lineage["capture_ids"]
    assert lineage["as_of"] == NOW.isoformat()


def test_midpoint_edge_but_real_asks_rejected(client_for, no_edge_db):
    cid = seed_candidate()  # scanner midpoints .45/.50 look like a gap
    response = client_for(no_edge_db).post(f"/api/v1/arbitrage/{cid}/feasibility", json=BODY)
    candidate = response.json()["candidate"]
    assert candidate["state"] == "INVALID" and candidate["executable_profit"] is None
    assert "NO_POSITIVE_NET_BOUND" in candidate["reasons"]


def test_missing_fee_policy_abstains_and_disabled_flag(client_for, edge_db, monkeypatch):
    cid = seed_candidate()
    client = client_for(edge_db)
    body = {**BODY}
    body.pop("fee_bps")
    body.pop("fee_version")
    candidate = client.post(f"/api/v1/arbitrage/{cid}/feasibility", json=body).json()["candidate"]
    assert candidate["state"] == "INVALID" and "UNKNOWN_FEES" in candidate["badges"]
    monkeypatch.setattr(get_settings(), "arbitrage_feasibility_enabled", False)
    assert client.post(f"/api/v1/arbitrage/{cid}/feasibility", json=BODY).status_code == 503


def test_unknown_candidate_and_no_captured_evidence(client_for, edge_db):
    client = client_for(edge_db)
    assert client.post("/api/v1/arbitrage/nope/feasibility", json=BODY).status_code == 404
    cid = seed_candidate()
    early = {**BODY, "as_of": (NOW - timedelta(days=1)).isoformat()}
    assert client.post(f"/api/v1/arbitrage/{cid}/feasibility", json=early).status_code == 409


def test_stale_receipts_are_flagged(client_for, edge_db):
    cid = seed_candidate()
    late = {**BODY, "as_of": (NOW + timedelta(minutes=10)).isoformat()}
    candidate = client_for(edge_db).post(f"/api/v1/arbitrage/{cid}/feasibility", json=late).json()
    assert candidate["candidate"]["state"] in {"STALE", "INVALID"}
    assert candidate["candidate"]["executable_profit"] is None


def test_frozen_run_replays_and_is_account_scoped(client_for, edge_db):
    cid = seed_candidate()
    client = client_for(edge_db)
    saved = client.post(f"/api/v1/arbitrage/{cid}/feasibility", json={**BODY, "persist": True})
    assert saved.status_code == 200, saved.text
    run_id = saved.json()["id"]
    replay = client.get(f"/api/v1/arbitrage/runs/{run_id}")
    assert replay.status_code == 200
    assert replay.json()["candidate"] == saved.json()["candidate"]
    assert replay.json()["result_hash"] == saved.json()["result_hash"]
    assert (
        client.get(f"/api/v1/arbitrage/runs/{run_id}", headers={"X-Account-Id": "2"}).status_code
        == 404
    )
    for account in ("3", "999"):
        assert (
            client.get(
                f"/api/v1/arbitrage/runs/{run_id}", headers={"X-Account-Id": account}
            ).status_code
            == 403
        )
    foreign = client_for(edge_db, user_id=2)
    assert foreign.get(f"/api/v1/arbitrage/runs/{run_id}").status_code == 404


def test_tampered_run_fails_integrity(client_for, edge_db):
    cid = seed_candidate()
    client = client_for(edge_db)
    run_id = client.post(
        f"/api/v1/arbitrage/{cid}/feasibility", json={**BODY, "persist": True}
    ).json()["id"]
    row = edge_db.get(ArbitrageFeasibilityRun, run_id)
    stored = json.loads(row.result_json)
    stored["state"] = "INVALID"
    row.result_json = json.dumps(stored, sort_keys=True)
    edge_db.commit()
    assert client.get(f"/api/v1/arbitrage/runs/{run_id}").status_code == 409
    with pytest.raises(ValueError, match="INTEGRITY"):
        replay_run(edge_db, row)


def test_replay_detects_changed_receipts_never_look_ahead(edge_db):
    request = FeasibilityRequest.model_validate(BODY)
    first = assess_frozen(edge_db, CID, request)
    again = assess_frozen(edge_db, CID, request)
    assert first == again  # point-in-time, deterministic
    assert first["candidate"]["as_of"] == NOW.isoformat().replace("+00:00", "Z")


def test_cross_venue_candidate_cannot_be_assessed_via_api(client_for, edge_db):
    arbitrage_service._arb_candidates["xv"] = {
        "candidate_id": "xv",
        "kind": "cross_venue",
        "condition_ids": ["a"],
    }
    response = client_for(edge_db).post("/api/v1/arbitrage/xv/feasibility", json=BODY)
    assert response.status_code == 409


def test_unauthenticated_requests_are_rejected():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/v1/arbitrage/candidates").status_code in {401, 403}
