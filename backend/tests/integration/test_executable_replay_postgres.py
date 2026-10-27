"""PostgreSQL locks, process rollback and additive migration preservation."""

import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_executable_replay import definition

from app.models import Account, BacktestRun, Base, User
from app.models.analytics.executable_replay import ExecutableReplayRun
from app.services.analytics.replay import executable_archive as archive


def test_concurrent_execute_crash_rollback_and_migration():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "executable_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(User(id=1, wallet_address="0x" + "11" * 20))
            db.flush()
            db.add(
                Account(id=1, owner_user_id=1, kind="personal", name="Personal", is_default=True)
            )
            db.add(
                BacktestRun(
                    user_id=1,
                    account_id=1,
                    strategy_type="indicator",
                    strategy_name="Preserved legacy",
                    start_date="2024-01-01",
                    end_date="2024-02-01",
                    status="completed",
                    total_pnl=7,
                )
            )
            db.commit()
        path = Path(__file__).parents[2] / "migrations/versions/20261027_0037_executable_replay.py"
        spec = importlib.util.spec_from_file_location("executable_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
                migration.upgrade()
                migration.upgrade()  # bootstrap's current metadata remains supported
            assert conn.execute(text("SELECT total_pnl FROM backtest_runs")).scalar() == 7
        with Session(engine) as db:
            frozen = archive.freeze(db, db.get(Account, 1), definition())
            identity = frozen.id
            db.commit()
        barrier = Barrier(2)

        def execute(_):
            with Session(engine) as db:
                account = db.get(Account, 1)
                row = db.get(ExecutableReplayRun, identity)
                barrier.wait(timeout=15)
                row = archive.execute(db, account, row)
                result = row.result_hash
                db.commit()
                return result

        with ThreadPoolExecutor(2) as executor:
            hashes = list(executor.map(execute, [1, 2]))
        assert hashes[0] == hashes[1]
        with Session(engine) as db:
            assert db.query(BacktestRun).count() == 2
            crash = archive.freeze(db, db.get(Account, 1), definition(hypothesis="crash rollback"))
            crash_id = crash.id
            db.commit()
        code = """
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Account
from app.models.analytics.executable_replay import ExecutableReplayRun
from app.services.analytics.replay import executable_archive as archive
engine = create_engine(os.environ["TEST_DATABASE_URL"],
    connect_args={"options": "-csearch_path=" + os.environ["REPLAY_TEST_SCHEMA"]})
with Session(engine) as db:
    row = db.get(ExecutableReplayRun, os.environ["REPLAY_TEST_ID"])
    archive.execute(db, db.get(Account, 1), row)
    os._exit(23)
"""
        backend = Path(__file__).parents[2]
        env = {
            **os.environ,
            "REPLAY_TEST_SCHEMA": schema,
            "REPLAY_TEST_ID": crash_id,
            "PYTHONPATH": str(backend) + ":" + str(backend.parents[0] / "services/shared"),
        }
        process = subprocess.run(  # noqa: S603 - fixed test code, isolated schema
            [sys.executable, "-c", code], cwd=backend, env=env, capture_output=True, timeout=45
        )
        assert process.returncode == 23, process.stderr.decode()
        with Session(engine) as db:
            row = db.get(ExecutableReplayRun, crash_id)
            assert row.result_json is None and row.backtest_run_id is None
            assert db.query(BacktestRun).count() == 2
            archive.execute(db, db.get(Account, 1), row)
            db.commit()
            assert db.query(BacktestRun).count() == 3
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
