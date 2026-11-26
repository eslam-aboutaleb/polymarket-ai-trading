"""Run the product API, account isolation and no-write checks on PostgreSQL."""

import os
from uuid import uuid4

import book_research_fixtures as fixtures
import pytest
import test_valuation_recorded_evidence as evidence
from sqlalchemy import create_engine, text
from test_settlement_recorded import exercise_recorded


def test_recorded_settlement_postgres(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "settlement_acceptance_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    monkeypatch.setattr(fixtures, "create_engine", lambda *a, **kw: engine)
    try:
        for db, _, inp in evidence.valuation_db.__wrapped__(monkeypatch):
            exercise_recorded(db, inp, monkeypatch)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
