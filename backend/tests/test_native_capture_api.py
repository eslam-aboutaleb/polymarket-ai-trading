"""Exercise real capture/replay routes with owned accounts and a native HTTP boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_follower_replay_api import client as follower_client  # noqa: F401
from test_native_capture import NOW, WALLET, venue_response

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import replay_evidence as evidence
from app.models.market_data.replay_capture import ReplayCapture
from app.services.market_data.replay_capture import digest


@pytest.fixture
def client(follower_client, monkeypatch):  # noqa: F811 - pytest injects the imported fixture
    http, factory, app = follower_client
    ReplayCapture.__table__.create(factory.kw["bind"])
    app.include_router(evidence.router)
    monkeypatch.setattr(
        evidence, "get_settings", lambda: SimpleNamespace(native_capture_enabled=True)
    )
    monkeypatch.setattr(evidence.limiter, "enabled", False)

    async def venue():
        async with httpx.AsyncClient(transport=httpx.MockTransport(venue_response)) as transport:
            yield evidence.PublicVenueCapture(transport)

    app.dependency_overrides[evidence.get_capture_venue] = venue
    return http, factory, app


def body(**changes):
    return {
        "wallet": WALLET,
        "start_at": (NOW - timedelta(minutes=1)).isoformat(),
        "end_at": datetime.now(UTC).isoformat(),
        "fee_bps": "0",
        "delays_seconds": [0],
        "dataset": {"source": "ignored client input", "signals": [], "books": []},
        **changes,
    }


def test_real_native_capture_saved_manifest_and_account_isolation(client):
    http, _, _ = client
    captured = http.post("/api/replay/capture", json={"wallet": WALLET})
    assert captured.status_code == 200, captured.text
    payload = body(end_at=captured.json()["captured_at"])
    result = http.post("/api/copy-trading/experiments/from-capture", json=payload)
    assert result.status_code == 200, result.text
    run = result.json()
    assert run["result"]["quality"] == "native_captured_modeled"
    assert run["result"]["execution_eligible"] is False
    assert digest(run["result"]["capture_manifest"]) == run["result"]["capture_manifest_hash"]
    assert all(row["content_hash"] for row in run["result"]["capture_manifest"])
    assert run["result"]["historical_backfill"] is False
    assert run["result"]["scenarios"][0]["valuation"]["nav"] == "999.75"
    captures = [http.get("/api/replay/captures/" + r).json() for r in run["result"]["capture_ids"]]
    assert {r["kind"] for r in captures} == {"signal", "book", "gamma", "clob_market"}
    assert run["input"]["dataset"]["signals"] and payload["dataset"]["signals"] == []
    assert http.get("/api/copy-trading/experiments/" + run["id"]).json() == run
    assert (
        http.get(
            "/api/copy-trading/experiments/" + run["id"], headers={"X-Account-Id": "2"}
        ).status_code
        == 404
    )
    assert (
        http.get(
            "/api/copy-trading/experiments/" + run["id"], headers={"X-Account-Id": "3"}
        ).status_code
        == 403
    )
    assert (
        http.post(
            "/api/replay/capture", json={"wallet": WALLET}, headers={"X-Account-Id": "3"}
        ).status_code
        == 403
    )


def test_capture_auth_flag_and_invalid_wallet_fail_closed(client, monkeypatch):
    http, _, app = client
    assert http.post("/api/replay/capture", json={"wallet": "../"}).status_code == 422
    monkeypatch.setattr(
        evidence, "get_settings", lambda: SimpleNamespace(native_capture_enabled=False)
    )
    assert http.post("/api/replay/capture", json={"wallet": WALLET}).status_code == 503
    app.dependency_overrides.pop(get_current_user_from_token)
    assert http.post("/api/replay/capture", json={"wallet": WALLET}).status_code == 401
    assert http.get("/api/replay/captures/not-present").status_code == 401


def test_capture_failure_no_history_and_future_end_never_create_run(client):
    http, factory, app = client
    assert http.post("/api/copy-trading/experiments/from-capture", json=body()).status_code == 409
    assert (
        http.post(
            "/api/copy-trading/experiments/from-capture",
            json=body(end_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()),
        ).status_code
        == 422
    )

    async def broken():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        ) as transport:
            yield evidence.PublicVenueCapture(transport)

    app.dependency_overrides[evidence.get_capture_venue] = broken
    assert http.post("/api/replay/capture", json={"wallet": WALLET}).status_code == 503
    assert http.get("/api/copy-trading/experiments").json() == {"runs": []}
    with factory() as db:
        assert db.query(ReplayCapture).count() == 0


def test_storage_failure_rolls_back_entire_capture_including_nested_signal(client):
    from unittest.mock import patch

    from sqlalchemy.exc import SQLAlchemyError

    http, factory, _ = client
    with patch.object(factory.class_, "commit", side_effect=SQLAlchemyError("private diagnostic")):
        response = http.post("/api/replay/capture", json={"wallet": WALLET})
    assert response.status_code == 503
    assert "private diagnostic" not in response.text
    with factory() as db:
        assert db.query(ReplayCapture).count() == 0
