"""Durable source capture, as-of exclusion, isolation and migration acceptance."""

import importlib.util
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_market_context import CID, NOW, RAW

from app.api.routes.analysis import analysis
from app.models.market_data.market_observation import MarketObservation
from app.security.rate_limit import limiter
from app.services.intelligence.market_context import build_market_context
from app.services.market_data.market_observations import append_observation, observation_history
from app.utils.database import get_db


@pytest.fixture
def api(market_observation_db):
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(analysis.router)
    app.dependency_overrides[get_db] = lambda: market_observation_db
    app.dependency_overrides[analysis.get_optional_user_from_token] = lambda: None
    limiter.reset()
    yield TestClient(app)
    limiter.reset()


def test_append_is_idempotent_and_previous_rules_are_preserved(market_observation_db):
    old = build_market_context(CID, RAW, NOW)
    new = build_market_context(
        CID, {**RAW, "description": "Revised resolution rule"}, NOW + timedelta(hours=1)
    )
    append_observation(market_observation_db, old)
    append_observation(market_observation_db, old)
    append_observation(market_observation_db, new)
    assert market_observation_db.query(MarketObservation).count() == 2
    history = observation_history(market_observation_db, CID, NOW + timedelta(hours=2), 25)
    assert [row.rules.text for row in history] == ["Revised resolution rule", RAW["description"]]
    assert history[0].rules.content_hash != history[1].rules.content_hash
    assert history[1].snapshot_id == old.snapshot_id


def test_late_receipt_never_leaks_into_an_earlier_decision(market_observation_db):
    # Even unchanged source text cannot appear before the server received it.
    first = build_market_context(CID, RAW, NOW)
    late = build_market_context(CID, RAW, NOW + timedelta(hours=3))
    append_observation(market_observation_db, first)
    append_observation(market_observation_db, late)
    visible = observation_history(
        market_observation_db, CID.upper().replace("0X", "0x"), NOW + timedelta(hours=1), 25
    )
    assert [row.snapshot_id for row in visible] == [first.snapshot_id]
    assert observation_history(market_observation_db, CID, NOW - timedelta(seconds=1), 25) == []
    assert (
        observation_history(
            market_observation_db, CID, NOW.astimezone(timezone(timedelta(hours=2))), 25
        )[0].snapshot_id
        == first.snapshot_id
    )


def test_other_market_is_never_included_and_limit_is_respected(market_observation_db):
    other_id = "0x" + "cd" * 32
    append_observation(market_observation_db, build_market_context(CID, RAW, NOW))
    append_observation(
        market_observation_db, build_market_context(CID, RAW, NOW + timedelta(seconds=1))
    )
    append_observation(
        market_observation_db,
        build_market_context(other_id, {**RAW, "condition_id": other_id}, NOW),
    )
    assert len(observation_history(market_observation_db, CID, NOW + timedelta(hours=1), 1)) == 1
    assert all(
        row.condition_id == CID
        for row in observation_history(market_observation_db, CID, NOW + timedelta(hours=1), 100)
    )


def test_conflicting_snapshot_identity_cannot_overwrite_stored_content(market_observation_db):
    original = build_market_context(CID, RAW, NOW)
    append_observation(market_observation_db, original)
    corrupted = original.model_copy(update={"question": "Different question"})
    with pytest.raises(ValueError, match="conflicts"):
        append_observation(market_observation_db, corrupted)
    assert (
        market_observation_db.get(MarketObservation, original.snapshot_id).payload_json
        == original.model_dump_json()
    )


def test_persisted_context_survives_new_session(market_observation_db):
    original = build_market_context(CID, RAW, NOW)
    append_observation(market_observation_db, original)
    with Session(market_observation_db.get_bind()) as fresh:
        assert observation_history(fresh, CID, NOW, 25)[0] == original


def test_context_capture_and_as_of_query_through_public_api(api):
    source = AsyncMock()
    source.get_market_outcome_tokens.return_value = RAW
    with (
        patch.object(analysis, "get_polymarket_service", return_value=source),
        patch("app.services.intelligence.market_context.utc_now", return_value=NOW),
    ):
        captured = api.get(f"/api/analysis/markets/{CID}/context")
    assert captured.status_code == 200
    path = f"/api/analysis/markets/{CID}/observations"
    response = api.get(path, params={"as_of": NOW.isoformat()})
    assert response.status_code == 200
    assert response.json()["observations"][0] == captured.json()
    assert api.get(path, params={"as_of": (NOW - timedelta(seconds=1)).isoformat()}).json() == {
        "observations": []
    }
    assert api.get(path, params={"as_of": "2026-10-22T00:00:00"}).status_code == 422
    assert api.get(path, params={"limit": 101}).status_code == 422


def test_migration_preserves_existing_data_and_model_schema_matches():
    path = Path(__file__).parents[1] / "migrations/versions/20261022_0029_market_observations.py"
    spec = importlib.util.spec_from_file_location("observation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE existing_data (value TEXT)"))
        connection.execute(text("INSERT INTO existing_data VALUES ('keep')"))
        with patch.object(module, "op", Operations(MigrationContext.configure(connection))):
            module.upgrade()
            assert {
                c["name"] for c in inspect(connection).get_columns("market_observations")
            } == set(MarketObservation.__table__.columns.keys())
            assert connection.execute(text("SELECT value FROM existing_data")).scalar() == "keep"
            module.downgrade()
            assert "market_observations" not in inspect(connection).get_table_names()
            assert connection.execute(text("SELECT value FROM existing_data")).scalar() == "keep"
    engine.dispose()


def test_capture_storage_failure_rolls_back_and_never_starts_generation(api, market_observation_db):
    source = AsyncMock()
    source.get_market_outcome_tokens.return_value = RAW
    with (
        patch.object(analysis, "get_polymarket_service", return_value=source),
        patch.object(
            analysis,
            "append_observation",
            side_effect=SQLAlchemyError("private storage diagnostic"),
        ),
        patch.object(
            market_observation_db, "rollback", wraps=market_observation_db.rollback
        ) as rollback,
        patch.object(analysis, "get_analysis_client_for_user", AsyncMock()) as factory,
    ):
        response = api.get(f"/api/analysis/markets/{CID}/context")
        assert response.status_code == 503
        research = api.post("/api/analysis/market/research/stream", json={"condition_id": CID})
        assert research.status_code == 503
        assert "private" not in response.text + research.text
        assert rollback.call_count == 2
        factory.assert_not_awaited()


def test_history_storage_failure_is_a_controlled_error(api):
    with patch.object(
        analysis, "observation_history", side_effect=SQLAlchemyError("private read error")
    ):
        response = api.get(f"/api/analysis/markets/{CID}/observations")
    assert response.status_code == 503
    assert "private" not in response.text
