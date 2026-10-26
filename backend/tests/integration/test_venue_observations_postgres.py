"""PostgreSQL first-receipt races and additive migration rollback."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data.venue_observations import (
    append_event,
    freeze_dataset,
    prune_unpinned,
    replay_dataset,
)


def test_concurrent_receipts_and_migration_preserve_existing_tables():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "observations_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    path = Path(__file__).parents[2] / "migrations/versions/20261027_0034_venue_observations.py"
    spec = importlib.util.spec_from_file_location("observation_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE preserved (id integer PRIMARY KEY)"))
            connection.execute(text("INSERT INTO preserved VALUES (1)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
        barrier = Barrier(2)
        moment = datetime.now(UTC)

        def insert(index):
            with Session(engine) as db:
                barrier.wait(timeout=15)
                row = append_event(
                    db,
                    {
                        "asset_id": "11",
                        "event_type": "last_trade_price",
                        "price": "0.4",
                        "size": "10",
                        "timestamp": "1",
                    },
                    received_at=moment,
                    connection_id=str(index),
                )
                result = row.id
                db.commit()
                return result

        with ThreadPoolExecutor(2) as executor:
            identities = list(executor.map(insert, range(2)))
        assert identities[0] == identities[1]
        with Session(engine) as db:
            assert db.query(VenueObservation).count() == 1
        barrier = Barrier(2)

        def freeze(_):
            with Session(engine) as db:
                barrier.wait(timeout=15)
                result = freeze_dataset(db, "11", moment)
                db.commit()
                return result

        with ThreadPoolExecutor(2) as executor:
            manifests = list(executor.map(freeze, range(2)))
        assert manifests[0] == manifests[1]
        with Session(engine) as db:
            assert len(replay_dataset(db, manifests[0]["dataset_id"])["observations"]) == 1
            assert prune_unpinned(db, datetime.now(UTC)) == 0
            db.commit()
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert "venue_observations" not in inspect(connection).get_table_names()
            assert connection.execute(text("SELECT count(*) FROM preserved")).scalar() == 1
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
