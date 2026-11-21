"""Upgrade/rollback preserve existing financial history and exact owned observations."""

import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models import Base
from app.models.analytics.live_balance_snapshot import LiveBalanceSnapshot
from app.models.users.account import Account
from app.models.users.user import User
from app.services.analytics.live_account_reconciliation import canonical, digest, read_snapshot


def test_upgrade_roundtrip_and_rollback_preserve_existing_account_history():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL acceptance")
    schema = "live_accounting_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    path = Path(__file__).parents[2] / "migrations/versions/20261121_0040_live_balance_snapshots.py"
    spec = importlib.util.spec_from_file_location("live_accounting_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        Base.metadata.create_all(
            engine,
            tables=[t for t in Base.metadata.sorted_tables if t.name != "live_balance_snapshots"],
        )
        with Session(engine) as db:
            db.add(User(id=1, wallet_address="0x" + "1" * 40))
            db.flush()
            db.add(Account(id=11, owner_user_id=1, name="Preserved", kind="personal"))
            db.commit()
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.upgrade()
        now = datetime.now(UTC)
        evidence = {"user_id": 1, "account_id": 11, "cash_usdc": "123.000001", "positions": {}}
        identity = digest(evidence)
        with Session(engine) as db:
            db.add(
                LiveBalanceSnapshot(
                    id=identity,
                    user_id=1,
                    account_id=11,
                    observed_at=now,
                    captured_at=now,
                    evidence_json=canonical(evidence),
                )
            )
            db.commit()
        with Session(engine) as db:
            assert read_snapshot(db, db.get(Account, 11), identity) == evidence
            assert db.get(LiveBalanceSnapshot, identity).observed_at == now
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.downgrade()
        assert "live_balance_snapshots" not in inspect(engine).get_table_names()
        with Session(engine) as db:
            assert db.get(Account, 11).name == "Preserved"
            assert db.get(User, 1).wallet_address == "0x" + "1" * 40
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
