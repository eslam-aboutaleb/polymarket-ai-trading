"""HTTP contract for structured market analysis (#33): validation, SSE, isolation."""

import contextlib
import json
from unittest.mock import patch

import pytest
from deep_dive_fixtures import CID, GOOD_CLAIMS, GOOD_CONTEXT, FakeClient, Venue, draft
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.analysis import analysis
from app.api.routes.auth.auth import get_current_user_from_token, get_optional_user_from_token
from app.main import app
from app.models import Base, User
from app.security.rate_limit import limiter
from app.services.market_data.replay_capture import PublicVenueCapture
from app.utils.database import get_db

V2 = {"output_format": "structured_v2", "condition_id": CID}


@pytest.fixture
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(
            [
                User(id=1, wallet_address="0x" + "11" * 20),
                User(id=2, wallet_address="0x" + "22" * 20),
            ]
        )
        seed.commit()
    current = {"user": {"user_id": 1}}
    venue = Venue()
    client = FakeClient(analysis=draft(*GOOD_CLAIMS), context=GOOD_CONTEXT)

    def get_session():
        with Session(engine) as session:
            yield session

    @contextlib.asynccontextmanager
    async def fixture_venue():
        async with venue.client() as http:
            yield PublicVenueCapture(http)

    async def fixture_client(user_id, db):
        return client, None

    app.dependency_overrides[get_db] = get_session
    app.dependency_overrides[get_current_user_from_token] = lambda: current["user"]
    app.dependency_overrides[get_optional_user_from_token] = lambda: current["user"]
    limiter.reset()
    with (
        patch.object(analysis, "deep_dive_venue", fixture_venue),
        patch.object(analysis, "get_analysis_client_for_user", fixture_client),
        contextlib.nullcontext(TestClient(app)) as http_client,
    ):
        yield http_client, venue, current
    app.dependency_overrides.clear()
    engine.dispose()


def frames(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def test_stream_emits_ordered_typed_frames_and_one_done(env):
    http, _, _ = env
    response = http.post("/api/analysis/market/stream", json=V2)
    assert response.status_code == 200
    out = frames(response)
    types = [f.get("type") for f in out if "type" in f]
    assert types == [
        "context_ready",
        "research_started",
        "evidence_collected",
        "claims_checked",
        "analysis_completed",
    ]
    assert [f for f in out if f.get("done")] == [{"done": True}]
    assert out[-1] == {"done": True}
    assert out[-2]["result"]["execution_eligible"] is False


def test_v2_rejects_client_supplied_market_data(env):
    http, _, _ = env
    for extra in (
        {"market_title": "Edited title"},
        {"yes_price": 0.5},
        {"end_date": "2030-01-01"},
        {"volume_24h": 10},
    ):
        assert http.post("/api/analysis/market/stream", json={**V2, **extra}).status_code == 422


def test_v2_requires_condition_id_and_legacy_rejects_it(env):
    http, _, _ = env
    assert (
        http.post("/api/analysis/market/stream", json={"output_format": "structured_v2"})
    ).status_code == 422
    legacy = {"market_title": "T", "market_description": "D", "yes_price": 0.4, "no_price": 0.6}
    assert (
        http.post("/api/analysis/market/stream", json={**legacy, "condition_id": CID}).status_code
        == 422
    )


def test_bad_token_is_422_before_the_stream_opens(env):
    http, _, _ = env
    response = http.post("/api/analysis/market/stream", json={**V2, "selected_token_id": "999"})
    assert response.status_code == 422
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_gamma_outage_is_503_not_a_partial_stream(env):
    http, venue, _ = env
    venue.fail_gamma = True
    assert http.post("/api/analysis/market/stream", json=V2).status_code == 503


def test_non_stream_returns_the_typed_result_and_persists_it(env):
    http, _, _ = env
    response = http.post("/api/analysis/market", json=V2)
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "structured_v2"
    analysis_id = body["data"]["analysis_id"]
    stored = http.get(f"/api/analysis/deep-dive/runs/{analysis_id}")
    assert stored.status_code == 200
    assert stored.json()["analysis_id"] == analysis_id


def test_flag_off_returns_503(env):
    http, _, _ = env
    with patch.object(analysis.settings, "market_deep_dive_v2_enabled", False):
        assert http.post("/api/analysis/market/stream", json=V2).status_code == 503
        assert http.post("/api/analysis/market", json=V2).status_code == 503


def test_runs_are_owner_isolated(env):
    http, _, current = env
    analysis_id = http.post("/api/analysis/market", json=V2).json()["data"]["analysis_id"]
    assert [r["analysis_id"] for r in http.get("/api/analysis/deep-dive/runs").json()["runs"]] == [
        analysis_id
    ]
    current["user"] = {"user_id": 2}
    assert http.get("/api/analysis/deep-dive/runs").json()["runs"] == []
    assert http.get(f"/api/analysis/deep-dive/runs/{analysis_id}").status_code == 404
    assert http.get("/api/analysis/deep-dive/runs/quality").status_code == 200
    assert http.get("/api/analysis/deep-dive/runs/not-a-uuid").status_code == 422
