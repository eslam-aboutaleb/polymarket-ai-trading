"""Persist real adapter results across additive rule migration and rollback on PostgreSQL."""

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
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models.market_data.venue_observation import VenueObservation
from app.services.intelligence.resolution_rules import (
    get_rule_snapshot_as_of,
    parse_resolution_rule,
    record_rule_snapshot,
)
from app.services.market_data import provider_contract as pc
from app.services.market_data.venue_observations import serialize


def migration(name):
    path = Path(__file__).parents[2] / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_receipts_survive_rule_schema_upgrade_and_rollback(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL migration acceptance")
    schema = "resolution_provider_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    archive = migration("20261027_0034_venue_observations.py")
    rules = migration("20261113_0039_resolution_rule_snapshots.py")
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            archive.upgrade()
        now = datetime.now(UTC)
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
                identities.append(pc.record_provider_probe(db, "12345", report))
                db.commit()

        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            rules.upgrade()
            rules.upgrade()

        fixture = Path(__file__).parents[1] / "fixtures" / "resolution_rule_btc_threshold.json"
        raw_market = json.loads(fixture.read_text())
        cid = raw_market["condition_id"]
        first = parse_resolution_rule(cid, raw_market, now)
        changed = json.loads(fixture.read_text())
        changed["tokens"].reverse()
        changed["tokens"][0]["token_id"], changed["tokens"][1]["token_id"] = (
            changed["tokens"][1]["token_id"],
            changed["tokens"][0]["token_id"],
        )
        second = parse_resolution_rule(cid, changed, now + timedelta(hours=1))
        with Session(engine) as db:
            record_rule_snapshot(db, first)
            record_rule_snapshot(db, first)
            record_rule_snapshot(db, second)
            db.commit()
        with Session(engine) as db:
            assert get_rule_snapshot_as_of(db, cid, now).source_hash == first.source_hash
            assert (
                get_rule_snapshot_as_of(db, cid, now + timedelta(hours=2)).source_hash
                == second.source_hash
            )
            assert get_rule_snapshot_as_of(db, cid, now - timedelta(seconds=1)) is None
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
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                rules.downgrade()
            assert "resolution_rule_snapshots" not in inspect(connection).get_table_names()
            assert connection.scalar(text("SELECT count(*) FROM venue_observations")) == 4
            with Operations.context(MigrationContext.configure(connection)):
                rules.upgrade()
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
