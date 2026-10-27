"""Maker account serialization, hard crash rollback and additive migrations."""

import asyncio
import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import httpx
from alembic.migration import MigrationContext
from alembic.operations import Operations
from book_research_fixtures import CID, NOW, venue_handler
from sqlalchemy import inspect
from test_paper_accounts_postgres import sessions as _sessions
from test_paper_maker_runtime import QUOTES, flow

from app.config import get_settings
from app.models import MarketMakerConfig, PaperAccount, PaperMakerFill, PaperMakerOrder, User
from app.services.analytics import paper_accounting as paper
from app.services.market_data import replay_capture
from app.services.market_data.book_research import capture_market_books
from app.services.market_data.replay_capture import PublicVenueCapture
from app.services.trading import paper_maker_runtime as runtime

sessions = _sessions


def test_concurrent_fill_crash_and_migration(sessions, monkeypatch):
    factory, url, schema = sessions
    for key in (
        "paper_maker_fill_model_v2",
        "binary_book_research_enabled",
        "observation_archive_enabled",
    ):
        monkeypatch.setattr(get_settings(), key, True)
    monkeypatch.setattr(get_settings(), "paper_maker_queue_ahead", Decimal(3))
    monkeypatch.setattr(replay_capture, "utc_now", lambda: NOW)
    with factory() as db:
        db.get(PaperAccount, 1).fee_bps = 100
        config = MarketMakerConfig(
            id=1,
            user_id=1,
            account_id=1,
            enabled=True,
            condition_id=CID,
            token_id_yes="11",
            token_id_no="22",
        )
        db.add(config)

        async def seed():
            async with httpx.AsyncClient(transport=httpx.MockTransport(venue_handler)) as client:
                await capture_market_books(db, CID, PublicVenueCapture(client))

        asyncio.run(seed())
        runtime.sync(db, config, QUOTES, NOW)
        db.commit()
    fractional_shares = Decimal("5.123456789012345678")
    fractional_cash = Decimal(1000) - fractional_shares * Decimal("0.404")
    barrier = Barrier(2)

    def fill(_):
        with factory() as db:
            barrier.wait(timeout=15)
            with paper.transaction(db):
                state = paper.account_state(db, 1, 1)
                runtime.consume(
                    db,
                    state,
                    paper.restore(db, state),
                    [flow(shares=Decimal("8.123456789012345678"))],
                    NOW + timedelta(seconds=3),
                )
            db.commit()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(fill, [1, 2]))
    with factory() as db:
        assert db.query(PaperMakerFill).count() == 1
        assert db.query(PaperMakerOrder).one().cumulative_filled == fractional_shares
        assert paper.restore(db, db.get(PaperAccount, 1)).cash == fractional_cash
    script = """
import os
from datetime import timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.analytics import paper_accounting as paper
from app.services.trading import paper_maker_runtime as runtime
from test_paper_maker_runtime import flow
from book_research_fixtures import NOW
options="-csearch_path="+os.environ["MAKER_TEST_SCHEMA"]
options+=" -cstatement_timeout=10000 -clock_timeout=10000"
engine=create_engine(os.environ["MAKER_TEST_DSN"],connect_args={"options":options})
with Session(engine) as db, paper.transaction(db):
 state=paper.account_state(db,1,1)
 runtime.consume(db,state,paper.restore(db,state),[flow(id="crash-event",available_at=NOW+timedelta(seconds=4))],NOW+timedelta(seconds=5))
 os._exit(7)
"""
    env = {
        **os.environ,
        "MAKER_TEST_DSN": url,
        "MAKER_TEST_SCHEMA": schema,
        "PYTHONPATH": ".:tests:../services/shared",
    }
    result = subprocess.run(  # noqa: S603 - controlled test script
        [sys.executable, "-c", script],
        env=env,
        cwd=Path(__file__).parents[2],
        timeout=120,
        capture_output=True,
    )  # noqa: S603 - controlled test script
    assert result.returncode == 7, result.stderr.decode()[-1000:]
    with factory() as db:
        assert db.query(PaperMakerFill).count() == 1
        assert paper.restore(db, db.get(PaperAccount, 1)).cash == fractional_cash
    race = Barrier(2)

    def cancel_or_fill(action):
        with factory() as db:
            race.wait(timeout=15)
            with paper.transaction(db):
                state = paper.account_state(db, 1, 1)
                if action == "cancel":
                    runtime.cancel_config(
                        db, db.get(MarketMakerConfig, 1), NOW + timedelta(seconds=6)
                    )
                else:
                    event = flow(
                        id="race-fill",
                        occurred_at=NOW + timedelta(seconds=4),
                        available_at=NOW + timedelta(seconds=5),
                    )
                    runtime.consume(
                        db, state, paper.restore(db, state), [event], NOW + timedelta(seconds=6)
                    )
            db.commit()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(cancel_or_fill, ["cancel", "fill"]))
    with factory() as db:
        ledger = paper.restore(db, db.get(PaperAccount, 1))
        preserved_cash = ledger.cash
        assert preserved_cash in {fractional_cash, Decimal("995.96")}
        assert ledger.reserved_cash == 0
        assert ledger.positions["11"].shares in {fractional_shares, Decimal(10)}
        assert db.query(PaperMakerFill).count() in {1, 2}
    path = Path(__file__).parents[2] / "migrations/versions/20261027_0036_maker_lifecycle.py"
    spec = importlib.util.spec_from_file_location("maker_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with (
        factory.kw["bind"].begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        module.upgrade()
        module.downgrade()
        assert "paper_maker_orders" not in inspect(connection).get_table_names()
        module.upgrade()
    with factory() as db:
        assert db.query(User).count() == 1
        assert paper.restore(db, db.get(PaperAccount, 1)).cash == preserved_cash
