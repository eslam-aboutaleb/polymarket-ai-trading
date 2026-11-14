"""Product API joins persisted rules, native depth and calibration on PostgreSQL."""

import os
from uuid import uuid4

import book_research_fixtures as fixtures
import pytest
import test_valuation_recorded_evidence as evidence_fixtures
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.routes.analysis.valuation import router
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.utils.database import get_db


def database_dependency(session):
    def get_session():
        yield session

    return get_session


def test_persisted_valuation_api_replays_without_trusting_caller_fields(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL acceptance")
    schema = "valuation_acceptance_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    monkeypatch.setattr(fixtures, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(get_settings(), "binary_valuation_enabled", True)
    try:
        for db, _, inp in evidence_fixtures.valuation_db.__wrapped__(monkeypatch):
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db] = database_dependency(db)
            app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
            with TestClient(app) as client:
                payload = {
                    key: value
                    for key, value in inp.model_dump(mode="json").items()
                    if key
                    in {
                        "condition_id",
                        "token_id",
                        "outcome",
                        "shares",
                        "as_of",
                        "rules_hash",
                        "forecast_id",
                    }
                }
                forged = client.post(
                    "/api/valuation/binary", json={**payload, "rule_review_status": "verified"}
                )
                assert forged.status_code == 422
                first = client.post("/api/valuation/binary", json=payload)
                assert first.status_code == 200, first.text
                assert first.json()["status"] == "valued", first.text
                assert first.json()["ev_gross_per_share"] == "0.05"
                assert first.json()["bounded_kelly_fraction"] == "0"
                assert first.json()["provenance"]["calibration"]["sample_count"] == 20
                db.expire_all()
                second = client.post("/api/valuation/binary", json=payload)
                assert second.json() == first.json()
                payload["token_id"] = "22"
                bad = client.post("/api/valuation/binary", json=payload)
                assert bad.json()["status"] == "abstain"
                assert "ABSTAIN_TOKEN_RULE_MISMATCH" in bad.json()["abstain_reasons"]
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
