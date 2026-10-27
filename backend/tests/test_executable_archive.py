"""Real SQL/API freeze, costed execution, owner boundary and integrity checks."""

import json
from decimal import Decimal

import pytest
from book_research_fixtures import research_db as _research_db
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_executable_replay import definition

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading.executable_replay import router
from app.config import get_settings
from app.models import Account, BacktestRun
from app.models.analytics.executable_replay import ExecutableReplayRun
from app.services.analytics.replay import executable_archive as archive
from app.utils.database import get_db

research_db = _research_db


@pytest.fixture
def client(research_db, monkeypatch):
    db = research_db
    monkeypatch.setattr(get_settings(), "executable_replay_enabled", True)
    from app.security.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as client:
        yield client, db


def test_freeze_execute_reverify_idempotent_and_account_isolated(client):
    http, db = client
    body = {"definition": definition().model_dump(mode="json")}
    frozen = http.post("/api/backtesting/executable/runs", json=body)
    assert frozen.status_code == 200, frozen.text
    identity = frozen.json()["id"]
    url = f"/api/backtesting/executable/runs/{identity}"
    assert http.post("/api/backtesting/executable/runs", json=body).json()["id"] == identity
    assert http.get(url, headers={"X-Account-Id": "2"}).status_code == 404
    assert http.get(url, headers={"X-Account-Id": "3"}).status_code == 403
    assert http.get(url, headers={"X-Account-Id": "invalid"}).status_code == 400
    executed = http.post(url + "/execute")
    assert executed.status_code == 200, executed.text
    result = executed.json()["result"]
    assert result["execution_evidence"] == "imported_unverified_modeled"
    assert result["holdout"]["net_pnl"] == "0.50"
    assert http.post(url + "/execute").json()["result_hash"] == executed.json()["result_hash"]
    assert db.query(BacktestRun).count() == 1
    assert http.post(url + "/verify").json()["identical"] is True
    row = db.get(ExecutableReplayRun, identity)
    row.input_json = json.dumps({**json.loads(row.input_json), "hypothesis": "tampered"})
    db.commit()
    assert http.get(url).status_code == 409
    assert http.post(url + "/execute").status_code == 409


def test_frozen_result_tampering_rejected(research_db):
    db = research_db
    account = db.get(Account, 1)
    row = archive.freeze(db, account, definition())
    archive.execute(db, account, row)
    db.commit()
    row.result_json = "{}"
    db.commit()
    with pytest.raises(ValueError, match="result integrity"):
        archive.verify(row, db)


def test_unknown_costs_do_not_create_fills(research_db):
    db = research_db
    model = definition()
    model = model.model_copy(
        update={"trials": [model.trials[0].model_copy(update={"fee_bps": None})]}
    )
    row = archive.freeze(db, db.get(Account, 1), model)
    archive.execute(db, db.get(Account, 1), row)
    result = json.loads(row.result_json)
    assert result["holdout"]["trade_count"] == 0
    assert result["holdout"]["net_pnl"] == "0"


def test_native_signal_payload_is_verified_before_trusting_receipt(research_db):
    from test_executable_replay import NOW

    from app.services.market_data.replay_capture import append_capture

    db = research_db
    identity = "a" * 64
    raw = {
        "signal": {
            "event_id": identity,
            "token_id": "11",
            "first_seen_at": NOW.isoformat(),
            "source_at": NOW.isoformat(),
            "side": "BUY",
        },
        "venue": {
            "asset": "11",
            "conditionId": "condition",
            "side": "BUY",
            "proxyWallet": "0x" + "11" * 20,
            "type": "TRADE",
        },
    }
    row = append_capture(
        db,
        kind="signal",
        source_key=identity,
        raw=raw,
        observed_at=NOW,
        source_at=NOW,
        wallet="0x" + "11" * 20,
        token_id="11",
        condition_id="condition",
        rule_hash="rules",
        stable_id=identity,
    )
    assert archive._signal_payload(row)["side"] == "BUY"
    row.payload_json = json.dumps({**raw, "venue": {"asset": "other"}})
    with pytest.raises(ValueError, match="INTEGRITY"):
        archive._signal_payload(row)


def test_native_final_resolution_books_payout_at_receipt_not_earlier(research_db, monkeypatch):
    import asyncio
    from datetime import timedelta

    import httpx
    from book_research_fixtures import CID, NOW, clob_market, raw_book

    from app.models.market_data.venue_observation import VenueObservation
    from app.services.market_data import book_research, replay_capture
    from app.services.market_data.replay_capture import PublicVenueCapture

    db = research_db
    clock = [NOW + timedelta(seconds=1)]
    monkeypatch.setattr(book_research, "utc_now", lambda: clock[0])
    monkeypatch.setattr(replay_capture, "utc_now", lambda: clock[0])

    def transport(request):
        final = clock[0] >= NOW + timedelta(seconds=4)
        if request.url.path.endswith("/book"):
            return httpx.Response(200, json=raw_book(request.url.params["token_id"], clock[0]))
        if "gamma" in request.url.host:
            return httpx.Response(
                200,
                json=[
                    {
                        "conditionId": CID,
                        "active": not final,
                        "closed": final,
                        "enableOrderBook": not final,
                        "outcomes": ["Yes", "No"],
                        "clobTokenIds": ["11", "22"],
                        "outcomePrices": ["1", "0"],
                        "umaResolutionStatus": "resolved" if final else "proposed",
                    }
                ],
            )
        market = clob_market()
        if final:
            market.update(active=False, closed=True, accepting_orders=False)
            market["tokens"] = [
                {"token_id": "11", "outcome": "Yes", "winner": True},
                {"token_id": "22", "outcome": "No", "winner": False},
            ]
        return httpx.Response(200, json=market)

    async def capture():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            for second in (1, 4):
                clock[0] = NOW + timedelta(seconds=second)
                await book_research.capture_market_books(db, CID, PublicVenueCapture(client))
        db.commit()

    asyncio.run(capture())
    raw = definition().model_dump(mode="json")
    raw["tape"]["signals"] = [raw["tape"]["signals"][0]]
    raw["tape"]["signals"][0].update(condition_id=CID, token_id="11")
    from app.services.analytics.replay.executable_contracts import ReplayDefinition

    row = archive.freeze(
        db,
        db.get(Account, 1),
        ReplayDefinition.model_validate(raw),
        [r.id for r in db.query(VenueObservation).all()],
    )
    archive.execute(db, db.get(Account, 1), row)
    result = json.loads(row.result_json)
    assert result["execution_evidence"] == "native_captured_counterfactual"
    assert result["holdout"]["net_pnl"] == "5.00"
    points = result["holdout"]["nav"]
    assert points[3]["nav"] == "999.00"  # exit bid mark, before final receipt
    assert points[4]["nav"] == "1005.00"
    assert any(e["booked"] for e in result["holdout"]["settlements"])
    archive.verify(row, db)


def test_legacy_cache_cannot_bypass_frozen_account_boundary(client, monkeypatch):
    from app.api.routes.trading import backtesting

    http, db = client
    http.app.include_router(backtesting.router)
    monkeypatch.setattr(
        backtesting,
        "get_backtest_runs",
        lambda user, limit: db.query(BacktestRun).filter_by(user_id=user).all(),
    )
    monkeypatch.setattr(
        backtesting,
        "get_backtest_run",
        lambda identity, user: db.query(BacktestRun).filter_by(id=identity, user_id=user).first(),
    )
    row = archive.freeze(db, db.get(Account, 1), definition())
    archive.execute(db, db.get(Account, 1), row)
    db.commit()
    identity = row.backtest_run_id
    assert http.get(f"/api/backtesting/runs/{identity}").status_code == 200
    assert (
        http.get(f"/api/backtesting/runs/{identity}", headers={"X-Account-Id": "2"}).status_code
        == 404
    )
    assert http.get("/api/backtesting/runs", headers={"X-Account-Id": "2"}).json()["runs"] == []
    assert http.delete(f"/api/backtesting/runs/{identity}").status_code == 409
    assert db.query(BacktestRun).count() == 1


def test_native_follower_reconstructs_cash_and_never_reuses_leader_profit(research_db):
    from datetime import timedelta

    from book_research_fixtures import CID, NOW

    from app.models.market_data.venue_observation import VenueObservation
    from app.services.market_data.replay_capture import append_capture

    db = research_db
    wallet, identity = "0x" + "11" * 20, "b" * 64
    first_seen = NOW - timedelta(seconds=1)
    capture = append_capture(
        db,
        kind="signal",
        source_key=identity,
        stable_id=identity,
        observed_at=first_seen,
        source_at=first_seen,
        wallet=wallet,
        condition_id=CID,
        token_id="11",
        rule_hash="native-market-fingerprint",
        raw={
            "signal": {
                "event_id": identity,
                "token_id": "11",
                "side": "BUY",
                "first_seen_at": first_seen.isoformat(),
                "source_at": first_seen.isoformat(),
            },
            "venue": {
                "asset": "11",
                "conditionId": CID,
                "side": "BUY",
                "proxyWallet": wallet,
                "type": "TRADE",
                "pnl": 999999,
            },
        },
    )
    row = archive.freeze(
        db,
        db.get(Account, 1),
        definition(start_at=NOW - timedelta(seconds=2)),
        [r.id for r in db.query(VenueObservation).all()],
        wallet,
    )
    archive.execute(db, db.get(Account, 1), row)
    result = json.loads(row.result_json)
    assert result["source_evidence"]["signal_evidence"] == "native_first_receipt_captured"
    assert result["holdout"]["trade_count"] == 1
    assert Decimal(result["holdout"]["net_pnl"]) == Decimal("-2.666")
    assert result["holdout"]["outcomes"][0]["shares"] == "18.33"
    assert Decimal(result["holdout"]["valuation"]["cash"]) == Decimal("990.002")
    assert db.query(ExecutableReplayRun).count() == 1
    archive.verify(row, db)
    capture.payload_json = "{}"
    with pytest.raises(ValueError, match="INTEGRITY"):
        archive.verify(row, db)
