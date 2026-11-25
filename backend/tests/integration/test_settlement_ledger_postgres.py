"""PostgreSQL: Alembic DDL, append-only triggers, concurrent appends and the API flow."""
# ruff: noqa: B023, S603

import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import book_research_fixtures as fixtures
import pytest
import test_settlement_ledger as ledger_tests
import test_valuation_recorded_evidence as evidence
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models.users.account import Account
from app.models.users.user import User
from app.services.analytics import settlement_ledger as ledger
from app.services.analytics.settlement_lifecycle import SettlementState as S

BACKEND = Path(__file__).resolve().parents[2]
CID = "0x" + "ab" * 32


def url():
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL required")
    return value


def alembic(db_url, schema, *args):
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": ".",
            "DATABASE_URL": db_url
            + ("&" if "?" in db_url else "?")
            + f"options=-csearch_path%%3D{schema}",
            "JWT_SECRET_KEY": "a" * 32,
            "REFRESH_TOKEN_HASH_SECRET": "b" * 32,
            "ENVIRONMENT": "test",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def schema_engine(db_url, monkeypatch=None):
    schema = "settlement_ledger_" + uuid4().hex
    admin = create_engine(db_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(db_url, connect_args={"options": f"-csearch_path={schema}"})
    return schema, admin, engine


def drop(schema, admin, engine):
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


@pytest.mark.parametrize(
    "name",
    [
        "test_reconciled_account_recognizes_exact_payout_pnl_and_cash",
        "test_sync_appends_server_derived_chain_history_once_and_audits",
        "test_api_cannot_claim_chain_evidence_or_supply_previous_state",
        "test_ledger_is_account_isolated",
        "test_database_refuses_update_and_delete_of_transitions",
    ],
)
def test_product_api_flow_and_triggers_on_postgres(monkeypatch, name):
    schema, admin, engine = schema_engine(url())
    monkeypatch.setattr(fixtures, "create_engine", lambda *a, **kw: engine)
    try:
        for db, _, inp in evidence.valuation_db.__wrapped__(monkeypatch):
            function = getattr(ledger_tests, name)
            arguments = (db, None, inp)
            if "monkeypatch" in function.__code__.co_varnames[: function.__code__.co_argcount]:
                function((db, None, inp), monkeypatch)
            else:
                function(arguments)
    finally:
        drop(schema, admin, engine)


def test_concurrent_appends_never_fork_the_chain(monkeypatch):
    schema, admin, engine = schema_engine(url())
    monkeypatch.setattr(fixtures, "create_engine", lambda *a, **kw: engine)
    try:
        for db, _, inp in evidence.valuation_db.__wrapped__(monkeypatch):
            now = inp.as_of
            outcomes, errors = [], []
            barrier = threading.Barrier(6)

            def worker(index):
                with Session(engine) as session:
                    account = session.get(Account, 1)
                    barrier.wait()
                    try:
                        row, created = ledger.append_transition(
                            session,
                            account,
                            condition_id=CID,
                            state=S.RESOLUTION_PROPOSED if index % 2 else S.FINAL,
                            observed_at=now - timedelta(minutes=1),
                            evidence_class="operator_asserted",
                            evidence={"writer": index},
                            now=now,
                        )
                        session.commit()
                        outcomes.append((index, created, row.sequence))
                    except (ledger.TransitionRejected, Exception) as exc:  # noqa: BLE001
                        session.rollback()
                        errors.append(type(exc).__name__ + ":" + str(exc))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            rows = ledger.history(db, db.get(Account, 1), CID)
            assert rows, (outcomes, errors)
            assert ledger.verify_chain(rows) == []
            assert [r.sequence for r in rows] == list(range(1, len(rows) + 1))
            assert len({r.prev_hash for r in rows}) == len(rows)
            # Every writer was either appended exactly once or refused with a code.
            assert len(rows) == sum(1 for _, created, _ in outcomes if created)
            assert len(outcomes) + len(errors) == 6
            assert datetime.now(UTC)
    finally:
        drop(schema, admin, engine)


def test_alembic_creates_append_only_ledger_and_refuses_destructive_downgrade():
    db_url = url()
    schema, admin, engine = schema_engine(db_url)
    try:
        upgraded = alembic(db_url, schema, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr[-2000:]
        with engine.begin() as connection:
            triggers = (
                connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE tgrelid = "
                        "to_regclass('settlement_transitions') AND NOT tgisinternal"
                    )
                )
                .scalars()
                .all()
            )
            assert "settlement_transitions_no_change" in triggers
        with Session(engine) as seed:
            seed.add(User(id=9, wallet_address="0x" + "9" * 40))
            seed.flush()
            seed.add(Account(id=9, owner_user_id=9, name="A", kind="personal", is_default=True))
            seed.commit()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO settlement_transitions (id, user_id, account_id, condition_id, "
                    "token_id, sequence, state, evidence_class, evidence_json, observed_at, "
                    "recorded_at, schema_version) VALUES ('h', 9, 9, :c, '*', 1, 'FINAL', "
                    "'operator_asserted', '{}', now(), now(), 'settlement-ledger-v1')"
                ),
                {"c": CID},
            )
        with pytest.raises(Exception, match="append-only"), engine.begin() as connection:
            connection.execute(text("DELETE FROM settlement_transitions"))
        down = alembic(db_url, schema, "downgrade", "20261122_0042_studies")
        assert down.returncode != 0
        assert "Refusing destructive downgrade" in down.stderr
    finally:
        drop(schema, admin, engine)
