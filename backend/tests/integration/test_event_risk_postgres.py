"""Actual PostgreSQL account reader, partial-fill lifecycle and isolation."""

import os
from datetime import datetime
from uuid import uuid4

import book_research_fixtures as fixtures
import pytest
import test_valuation_recorded_evidence as evidence
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from test_recorded_event_risk import seed

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.event_risk import router
from app.config import get_settings
from app.utils.database import get_db


def dependency(session):
    def get():
        yield session

    return get


def test_recorded_risk_survives_replay_and_preserves_account_boundaries(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "event_risk_acceptance_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    monkeypatch.setattr(fixtures, "create_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(get_settings(), "event_correlation_risk_enabled", True)
    try:
        for db, _, inp in evidence.valuation_db.__wrapped__(monkeypatch):
            seed(db, inp)

            class Clock(datetime):
                frozen_time = inp.as_of

                @classmethod
                def now(cls, tz=None):
                    return cls.frozen_time.astimezone(tz) if tz else cls.frozen_time

            monkeypatch.setattr("app.services.analytics.event_exposure_read_side.datetime", Clock)
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db] = dependency(db)
            app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
            with TestClient(app) as client:
                first = client.get("/api/risk/event-correlation/recorded")
                assert first.status_code == 200, first.text
                assert first.json()["status"] == "assessed", first.text
                assert first.json()["execution_eligible"] is False
                db.expire_all()
                again = client.get("/api/risk/event-correlation/recorded")
                assert (
                    again.json()["worst_case_portfolio_loss"]
                    == first.json()["worst_case_portfolio_loss"]
                )
                assert (
                    client.get(
                        "/api/risk/event-correlation/recorded", headers={"X-Account-Id": "3"}
                    ).status_code
                    == 403
                )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
