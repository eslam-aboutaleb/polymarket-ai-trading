"""Archive/API lineage on actual PostgreSQL; source observations are explicit fixtures."""

import os
from uuid import uuid4

import book_research_fixtures as fixtures
import pytest
import test_valuation_recorded_evidence as evidence_fixtures
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from test_oracle_archive import archived_pair

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.oracle_basis import router
from app.config import get_settings
from app.utils.database import get_db


def dependency(session):
    def get():
        yield session

    return get


def test_reference_receipts_and_api_replay_on_postgres(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "oracle_acceptance_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    monkeypatch.setattr(fixtures, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(get_settings(), "oracle_basis_risk_enabled", True)
    try:
        for db, _, inp in evidence_fixtures.valuation_db.__wrapped__(monkeypatch):
            req = archived_pair(db, inp)
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db] = dependency(db)
            app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
            with TestClient(app) as client:
                response = client.post(
                    "/api/oracle-basis/analyze", json=req.model_dump(mode="json")
                )
                assert response.status_code == 200, response.text
                assert response.json()["oracle_verified"], response.text
                db.expire_all()
                assert (
                    client.post(
                        "/api/oracle-basis/analyze", json=req.model_dump(mode="json")
                    ).json()
                    == response.json()
                )
                payload = req.model_dump(mode="json")
                payload["settlement_observations"][0]["capture_id"] = None
                assert not client.post("/api/oracle-basis/analyze", json=payload).json()[
                    "oracle_verified"
                ]
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
