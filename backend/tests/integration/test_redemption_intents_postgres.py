"""PostgreSQL durability, process/session races and additive migration rollback."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from redemption_fixtures import CONDITION, TEST_KEY, TEST_WALLET, FundedRedemptionRPC
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models import Account, AppEvent, Base, TradingControlState, User, UserSettings
from app.models.trading.redemption_intent import RedemptionIntent
from app.services.trading import redemption_workflow as workflow


@pytest.fixture
def postgres_redemption():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "redemption_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1, wallet_address=TEST_WALLET))
        db.flush()
        db.add_all(
            [
                Account(id=11, owner_user_id=1, name="Preserved", kind="personal", is_default=True),
                UserSettings(user_id=1, simulation_mode=False, trading_halted=False),
                TradingControlState(scope="global", state="RUNNING", reason="synthetic fixture"),
                TradingControlState(
                    scope="user:1", user_id=1, state="RUNNING", reason="synthetic fixture"
                ),
            ]
        )
        db.commit()
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_concurrent_intent_and_submission_have_one_identity_nonce_and_send(
    postgres_redemption, monkeypatch
):
    engine, rpc = postgres_redemption, FundedRedemptionRPC()
    monkeypatch.setattr(workflow, "block_legacy_live_execution", lambda _: None)
    monkeypatch.setattr(workflow, "load_wallet_credentials", lambda _: {"private_key": TEST_KEY})
    barrier = Barrier(2)

    def create():
        native = rpc.native()
        try:
            with Session(engine) as db:
                account = db.get(Account, 11)
                barrier.wait(timeout=10)
                return workflow.create_intent(
                    db, account, CONDITION, "duplicate", mode="submit", chain=native
                ).id
        finally:
            native.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create(), range(2)))
    assert ids[0] == ids[1]
    barrier = Barrier(2)

    def submit():
        native = rpc.native()
        try:
            with Session(engine) as db:
                account = db.get(Account, 11)
                barrier.wait(timeout=10)
                return workflow.submit_intent(db, account, ids[0], chain=native).state
        finally:
            native.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: submit(), range(2)))
    assert len(rpc.broadcasts) == rpc.nonce_reads == 1
    rpc.final_number = 30
    barrier = Barrier(2)

    def reconcile():
        native = rpc.native()
        try:
            with Session(engine) as db:
                account = db.get(Account, 11)
                barrier.wait(timeout=10)
                return workflow.reconcile_intent(db, account, ids[0], chain=native).state
        finally:
            native.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: reconcile(), range(2)))
    with Session(engine) as db:
        assert db.query(RedemptionIntent).count() == 1
        assert db.query(AppEvent).filter_by(event_type="redemption_finalized").count() == 1
        assert db.get(RedemptionIntent, ids[0]).state == "confirmed"


def test_upgrade_rollback_preserves_legacy_history(postgres_redemption):
    engine = postgres_redemption
    path = Path(__file__).parents[2] / "migrations/versions/20261122_0043_redemption_intents.py"
    spec = importlib.util.spec_from_file_location("redemption_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Downgrade also covers Base.metadata/bootstrap-created named constraints.
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.downgrade()
        connection.execute(
            text(
                "INSERT INTO redemption_attempts "
                "(user_id, market_id, collection_id, status, amount, created_at) "
                "VALUES (1, 'legacy', 'unverified', 'legacy_unknown', 7, CURRENT_TIMESTAMP)"
            )
        )
        module.upgrade()
        module.upgrade()
        count = connection.execute(
            text(
                "SELECT count(*) FROM redemption_attempts "
                "WHERE market_id='legacy' AND amount=7 AND intent_id IS NULL"
            )
        ).scalar()
        assert count == 1
        module.downgrade()
    assert "redemption_intents" not in inspect(engine).get_table_names()
    with Session(engine) as db:
        assert db.get(Account, 11).name == "Preserved"
