"""Real database serialization, atomic restart and additive migration checks."""

import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from paper_account_fixtures import evidence
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import (
    Account,
    Base,
    PaperAccount,
    PaperJournalEvent,
    PaperNavPoint,
    PaperOrderRecord,
    User,
    UserSettings,
    UserTrade,
)
from app.services.analytics import paper_accounting as paper


@pytest.fixture
def sessions(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for PostgreSQL paper financial regressions")
    schema = "paper_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "paper_journal_v2_enabled", True)
    moment = datetime.now(UTC)
    with factory() as db:
        db.add(User(id=1, wallet_address="0x" + "11" * 20))
        db.flush()
        db.add(Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True))
        db.add(UserSettings(user_id=1, paper_balance=1000, simulation_mode=True))
        db.flush()
        paper.configure_account(db, 1, 1, fee_bps=0)
        evidence(db, [moment])
        db.commit()
    try:
        yield factory, url, schema
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_simultaneous_same_intent_survives_restart_once(sessions):
    factory, _, _ = sessions
    barrier = Barrier(2)

    def write(_):
        with factory() as db:
            barrier.wait(timeout=15)
            result = paper.submit_paper_order(
                db,
                user_id=1,
                account_id=1,
                token_id="11",
                side="BUY",
                amount=25,
                units="SHARES",
                strategy="copy",
                intent_key="same",
            )
            db.commit()
            return result

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, range(2)))
    assert results[0] == results[1]
    with factory() as restarted:
        assert restarted.query(UserTrade).count() == restarted.query(PaperOrderRecord).count() == 1
        assert restarted.query(PaperJournalEvent).count() == 2
        assert paper.restore(restarted, restarted.get(PaperAccount, 1)).cash == 990


def test_strategies_serialize_cash_and_shared_depth(sessions):
    factory, _, _ = sessions
    barrier = Barrier(2)

    def write(strategy):
        with factory() as db:
            barrier.wait(timeout=15)
            result = paper.submit_paper_order(
                db,
                user_id=1,
                account_id=1,
                token_id="11",
                side="BUY",
                amount=100,
                units="SHARES",
                strategy=strategy,
                intent_key=strategy,
            )
            db.commit()
            return result

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, ["copy", "latency_arb"]))
    assert sorted(result["status"] for result in results) == ["filled", "unfilled"]
    with factory() as db:
        ledger = paper.restore(db, db.get(PaperAccount, 1))
        assert ledger.cash == 960 and ledger.positions["11"].shares == 100


def test_process_crash_rolls_back_all_economic_rows(sessions):
    factory, url, schema = sessions
    source = """
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.config import get_settings
from app.services.analytics.paper_accounting import submit_paper_order
get_settings().paper_journal_v2_enabled = True
engine = create_engine(os.environ["TEST_DATABASE_URL"],
    connect_args={"options": "-csearch_path="+os.environ["PAPER_TEST_SCHEMA"]})
with Session(engine) as db:
    submit_paper_order(db, user_id=1, account_id=1, token_id="11", side="BUY",
        amount=25, units="SHARES", strategy="copy", intent_key="crash")
    os._exit(23)
"""
    process = subprocess.run(  # noqa: S603 - fixed test interpreter/source, no shell
        [sys.executable, "-c", source],
        env={**os.environ, "TEST_DATABASE_URL": url, "PAPER_TEST_SCHEMA": schema},
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 23, process.stderr.decode()
    with factory() as db:
        assert (
            db.query(PaperJournalEvent).count()
            == db.query(PaperOrderRecord).count()
            == db.query(UserTrade).count()
            == 0
        )
        assert paper.restore(db, db.get(PaperAccount, 1)).cash == 1000


def test_migration_upgrade_bootstrap_and_downgrade_preserve_legacy(sessions):
    factory, _, _ = sessions
    path = Path(__file__).parents[2] / "migrations/versions/20261026_0034_paper_accounts.py"
    spec = importlib.util.spec_from_file_location("paper_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = factory.kw["bind"]
    from app.models.trading.live_order import LiveOrder

    with factory() as db:
        legacy = UserTrade(
            user_id=1,
            account_id=1,
            market_id="legacy",
            token_id="11",
            action="buy",
            amount=5,
            price=0.5,
            status="simulated",
            pnl=1.25,
        )
        pending = UserTrade(
            user_id=1,
            account_id=1,
            market_id="live",
            token_id="11",
            action="buy",
            amount=2,
            price=0.5,
            status="pending",
        )
        db.add_all([legacy, pending])
        db.flush()
        db.add(
            LiveOrder(
                user_id=1,
                account_id=1,
                trade_id=pending.id,
                idempotency_key="preserved-live",
                fingerprint="f" * 64,
                strategy="manual",
                token_id="11",
                side="BUY",
                price=0.5,
                shares=4,
                notional=2,
                reserved=2,
                state="prepared",
            )
        )
        db.commit()
    with engine.begin() as connection:
        # Bootstrap metadata may already contain all new tables.
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.downgrade()
        assert "paper_accounts" not in inspect(connection).get_table_names()
        assert connection.execute(text("SELECT count(*) FROM users")).scalar() == 1
        assert connection.execute(text("SELECT count(*) FROM user_trades")).scalar() == 2
        assert (
            connection.execute(
                text("SELECT reserved FROM live_orders WHERE idempotency_key='preserved-live'")
            ).scalar()
            == 2
        )
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.upgrade()
        constraints = inspect(connection).get_unique_constraints("paper_nav_points")
        assert any(row["column_names"] == ["account_id", "bucket_hour"] for row in constraints)
        assert all(
            name in inspect(connection).get_table_names()
            for name in [
                PaperAccount.__tablename__,
                PaperJournalEvent.__tablename__,
                PaperOrderRecord.__tablename__,
                PaperNavPoint.__tablename__,
            ]
        )
