"""Real serialization and additive migration for frozen delayed paper decisions."""

import asyncio
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from alembic.migration import MigrationContext
from alembic.operations import Operations
from latency_paper_fixtures import native
from test_latency_paper_evidence import opportunity
from test_paper_accounts_postgres import sessions as _sessions

from app.models import UserSettings, UserTrade
from app.models.trading.paper_account import PaperOrderRecord
from app.models.trading.paper_execution_intent import PaperExecutionIntent
from app.services.analytics import paper_accounting as paper
from app.services.trading import latency_paper as runtime

sessions = _sessions


def test_concurrent_frozen_intent_books_once_after_restart(sessions, monkeypatch):
    factory, _, _ = sessions
    moment = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    clock = [moment]
    monkeypatch.setattr(runtime, "utc_now", lambda: moment)
    monkeypatch.setattr(paper, "utc_now", lambda: moment)
    with factory() as db:
        native(db, clock)
        db.commit()

    async def capture(*args, **kwargs):
        return True

    monkeypatch.setattr(runtime, "refresh_evidence", capture)
    barrier = Barrier(2)

    def execute(_):
        with factory() as db:
            settings = db.query(UserSettings).one()
            barrier.wait(timeout=15)
            return asyncio.run(runtime.execute(db, settings, opportunity(clock), "11", 10, 0))

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(execute, range(2)))
    assert results[0] == results[1]
    assert results[0]["execution_state"] == "ESTIMATED", results
    with factory() as db:
        assert db.query(PaperExecutionIntent).count() == 1
        assert db.query(PaperOrderRecord).count() == db.query(UserTrade).count() == 1
        assert paper.restore(db, paper.account_state(db, 1, 1)).cash == 990


def test_additive_migration_does_not_rewrite_orders(sessions):
    factory, _, _ = sessions
    path = (
        Path(__file__).parents[2] / "migrations/versions/20261027_0038_paper_execution_intents.py"
    )
    spec = importlib.util.spec_from_file_location("paper_intent_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with factory() as db:
        result = paper.submit_paper_order(
            db,
            user_id=1,
            token_id="11",
            side="BUY",
            amount=10,
            strategy="manual",
            intent_key="pre-migration",
        )
        db.commit()
        before = db.query(PaperOrderRecord).one().result_json
    with (
        factory.kw["bind"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        module.downgrade()
        module.upgrade()
        module.upgrade()
    with factory() as db:
        assert db.query(PaperOrderRecord).one().result_json == before
        assert db.query(PaperExecutionIntent).count() == 0
        assert result["order_id"] == db.query(PaperOrderRecord).one().id
