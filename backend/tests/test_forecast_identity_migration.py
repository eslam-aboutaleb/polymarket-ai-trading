"""Forecast identity migration is additive, idempotent and reversible (issue #60)."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.models.analytics.assessment import Assessment


def _load():
    path = Path(__file__).parents[1] / "migrations/versions/20261125_0060_forecast_identity.py"
    spec = importlib.util.spec_from_file_location("forecast_identity_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_migration_keeps_legacy_rows_and_round_trips():
    migration = _load()
    new_columns = {name for name, _ in migration._COLUMNS}
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE assessments (id INTEGER PRIMARY KEY, ai_probability FLOAT, "
                "condition_id VARCHAR(100))"
            )
        )
        connection.execute(text("INSERT INTO assessments VALUES (1, 0.6, '0xlegacy')"))
        with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
            migration.upgrade()
            names = {c["name"] for c in inspect(connection).get_columns("assessments")}
            assert new_columns <= names
            assert new_columns <= set(Assessment.__table__.columns.keys())
            assert connection.execute(
                text("SELECT ai_probability, request_key FROM assessments")
            ).one() == (0.6, None)
            indexes = {i["name"]: i for i in inspect(connection).get_indexes("assessments")}
            assert indexes["ix_assessments_request_key"]["unique"]
            assert indexes["ix_assessments_forecast_uuid"]["unique"]
            assert inspect(connection).has_table("forecast_resolution_events")
            # Legacy rows keep NULL identity and NULLs do not collide.
            connection.execute(text("INSERT INTO assessments (id, ai_probability) VALUES (2, 0.7)"))
            migration.upgrade()  # metadata bootstrap may already have created everything
            migration.downgrade()
            assert not inspect(connection).has_table("forecast_resolution_events")
            remaining = {c["name"] for c in inspect(connection).get_columns("assessments")}
            assert not (new_columns & remaining)
            assert connection.execute(text("SELECT COUNT(*) FROM assessments")).scalar() == 2
    engine.dispose()
