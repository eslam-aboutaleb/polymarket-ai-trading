"""Additive evidence migration handles both existing and metadata-bootstrap schemas."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from app.models.market_data.replay_capture import ReplayCapture


def test_migration_preserves_unrelated_rows_and_reupgrades():
    path = Path(__file__).parents[1] / "migrations/versions/20261023_0032_native_replay_capture.py"
    spec = importlib.util.spec_from_file_location("native_capture_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE existing_rows (value TEXT)"))
        connection.execute(text("INSERT INTO existing_rows VALUES ('keep')"))
        with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
            migration.upgrade()
            assert set(ReplayCapture.__table__.columns.keys()) == {
                c["name"] for c in inspect(connection).get_columns("replay_captures")
            }
            assert {i["name"] for i in inspect(connection).get_indexes("replay_captures")} == {
                "ix_replay_capture_wallet_clock",
                "ix_replay_capture_token_clock",
            }
            connection.execute(
                text(
                    "INSERT INTO replay_captures "
                    "(id,kind,source_key,observed_at,content_hash,payload_json) "
                    "VALUES ('id','book','key','2026-10-23','hash','{}')"
                )
            )
            migration.upgrade()  # The legacy metadata bootstrap may already create it.
            assert connection.execute(text("SELECT COUNT(*) FROM replay_captures")).scalar() == 1
            migration.downgrade()
            assert "replay_captures" not in inspect(connection).get_table_names()
            assert connection.execute(text("SELECT value FROM existing_rows")).scalar() == "keep"
            migration.upgrade()
            assert inspect(connection).has_table("replay_captures")
    engine.dispose()
