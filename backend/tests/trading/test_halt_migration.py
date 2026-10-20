import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from app.models import Base


def test_expand_migration_seeds_fail_closed_and_preserves_existing_halt():
    engine = create_engine("sqlite://")
    migration = importlib.import_module("migrations.versions.20261020_0022_trading_controls")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        # Exercise an existing pre-expand deployment as well as the historical
        # baseline's create_all path (covered by test_migrations_security).
        connection.execute(text("DROP TABLE emergency_halt_operations"))
        connection.execute(text("DROP TABLE trading_control_states"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            state = connection.execute(
                text("SELECT state, version FROM trading_control_states WHERE scope = 'global'")
            ).one()
            assert tuple(state) == ("RECOVERY_REQUIRED", 1)
            connection.execute(
                text("UPDATE trading_control_states SET state = 'HALTED', version = 2")
            )
            migration.upgrade()
            assert connection.scalar(text("SELECT state FROM trading_control_states")) == "HALTED"
            with pytest.raises(RuntimeError, match="halt history"):
                migration.downgrade()
    engine.dispose()


def test_empty_migration_can_downgrade_and_upgrade():
    engine = create_engine("sqlite://")
    migration = importlib.import_module("migrations.versions.20261020_0022_trading_controls")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()
            assert (
                connection.scalar(
                    text("SELECT state FROM trading_control_states WHERE scope = 'global'")
                )
                == "RECOVERY_REQUIRED"
            )
    engine.dispose()
