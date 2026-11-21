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
    report_spec = importlib.util.spec_from_file_location(
        "attribution_archive_migration", path.with_name("20261121_0041_attribution_reports.py")
    )
    report_migration = importlib.util.module_from_spec(report_spec)
    report_spec.loader.exec_module(report_migration)
    study_spec = importlib.util.spec_from_file_location(
        "attribution_study_migration", path.with_name("20261122_0042_attribution_studies.py")
    )
    study_migration = importlib.util.module_from_spec(study_spec)
    study_spec.loader.exec_module(study_migration)
    try:
        Base.metadata.create_all(
            engine,
            tables=[
                t
                for t in Base.metadata.sorted_tables
                if t.name
                not in {"live_balance_snapshots", "live_attribution_reports", "attribution_studies"}
            ],
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
            report_migration.upgrade()
            study_migration.upgrade()
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
            from test_attribution_acceptance import (
                test_cross_window_traceable_fifo as exercise_history,
            )

            exercise_history(db)
            from app.models.analytics.live_balance_snapshot import LiveAttributionReport
            from app.services.analytics.attribution_archive import freeze_report, read_report

            snapshots = (
                db.query(LiveBalanceSnapshot)
                .filter(LiveBalanceSnapshot.id != identity)
                .order_by(LiveBalanceSnapshot.observed_at)
                .all()
            )
            saved = freeze_report(db, db.get(Account, 11), snapshots[0].id, snapshots[-1].id)
            assert (
                freeze_report(db, db.get(Account, 11), snapshots[0].id, snapshots[-1].id) == saved
            )
            assert db.query(LiveAttributionReport).count() == 1
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier

            barrier = Barrier(2)
            opening_id, closing_id = snapshots[0].id, snapshots[-1].id

            def concurrent_save():
                with Session(engine) as concurrent_db:
                    owner = concurrent_db.get(Account, 11)
                    barrier.wait(timeout=10)
                    return freeze_report(concurrent_db, owner, opening_id, closing_id)

            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(concurrent_save) for _ in range(2)]
                assert [future.result(timeout=20) for future in pending] == [saved, saved]
            assert db.query(LiveAttributionReport).count() == 1
            assert read_report(db, db.get(Account, 11), saved["report_id"]) == saved
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            study_migration.downgrade()
            report_migration.downgrade()
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


def test_postgres_prospective_study_and_all_functional_acceptance_gates(monkeypatch):
    """Synthetic forward fixtures exercise PostgreSQL, not actual profitability."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "prospective_acceptance_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(User(id=1, wallet_address="0x" + "1" * 40))
            db.flush()
            db.add(
                Account(
                    id=11, owner_user_id=1, name="Synthetic study", kind="personal", is_default=True
                )
            )
            db.commit()
            from test_attribution_studies import (
                test_complete_disjoint_forward_acceptance as exercise_study,
            )

            exercise_study(db, monkeypatch)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
