"""Persist provider health/raw provenance without breaking pre-contract archived receipts."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data import provider_contract as pc
from app.services.market_data.venue_observations import append_event, serialize


def test_provider_upgrade_retains_legacy_history_and_raw_readiness_evidence(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL upgrade acceptance")
    schema = "provider_contract_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    path = Path(__file__).parents[2] / "migrations/versions/20261027_0034_venue_observations.py"
    spec = importlib.util.spec_from_file_location("provider_archive_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.upgrade()
        now = datetime.now(UTC)
        with Session(engine) as db:
            legacy = append_event(
                db,
                {
                    "event_type": "last_trade_price",
                    "asset_id": "12345",
                    "price": "0.420000000000000001",
                    "size": "1",
                },
                received_at=now - timedelta(days=1),
                connection_id="legacy-receipt",
            )
            legacy_id = legacy.id
            legacy_payload = legacy.payload_json
            db.commit()
        end = int(now.timestamp()) - 60
        bodies = [
            (200, {"history": [{"t": end - 3600 * i, "p": 0.52} for i in range(23, -1, -1)]}),
            (200, {"history": [{"t": end, "p": 0.52}]}),
            (429, {"error": "rate limited"}),
            (503, {"error": "unavailable"}),
        ]
        identities = []
        for status, body in bodies:
            raw = json.dumps(body).encode()
            monkeypatch.setattr(pc, "_LAST_CANARY_MONO", None)
            transport = httpx.MockTransport(
                lambda _, code=status, data=raw: httpx.Response(code, content=data)
            )
            report = asyncio.run(
                pc.run_provider_contract_canary("12345", enabled=True, transport=transport)
            )
            with Session(engine) as db:
                identity = pc.record_provider_probe(db, "12345", report)
                assert pc.record_provider_probe(db, "12345", report) == identity
                identities.append(identity)
                db.commit()
        # No new schema is needed: upgrading an already populated archive is idempotent.
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.upgrade()
        with Session(engine) as db:
            assert db.query(VenueObservation).count() == 5
            assert db.get(VenueObservation, legacy_id).payload_json == legacy_payload
            assert serialize(db.get(VenueObservation, legacy_id))["normalized"]["price"] == (
                "0.420000000000000001"
            )
            reports = []
            for identity in identities:
                value = serialize(db.get(VenueObservation, identity))
                raw = base64.b64decode(value["raw"]["raw_body_base64"])
                assert (
                    hashlib.sha256(raw).hexdigest() == value["raw"]["report"]["raw_checksum_sha256"]
                )
                reports.append(value["raw"]["report"])
            assert [r["health_state"] for r in reports] == [
                "READY",
                "DEGRADED",
                "RATE_LIMITED",
                "UNAVAILABLE",
            ]
            assert reports[1]["missing_bars"] == 23
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
