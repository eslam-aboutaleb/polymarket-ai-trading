"""Real concurrency, process rollback and additive migration preservation."""

import asyncio
import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from book_research_fixtures import CID, NOW, venue_handler
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models import Account, Base, BookResearchRun, User
from app.models.market_data.venue_observation import ObservationDataset
from app.services.market_data.book_research import (
    ResearchQuery,
    capture_market_books,
    replay_research,
    save_research,
)
from app.services.market_data.replay_capture import PublicVenueCapture


def test_concurrent_owned_freezes_crash_and_migration(monkeypatch):
    from app.services.market_data import book_research, replay_capture

    monkeypatch.setattr(book_research, "utc_now", lambda: NOW)
    monkeypatch.setattr(replay_capture, "utc_now", lambda: NOW)
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    schema = "book_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            for index in (1, 2):
                db.add(User(id=index, wallet_address="0x" + str(index) * 40))
            db.flush()
            for index in (1, 2):
                db.add(
                    Account(
                        id=index,
                        owner_user_id=index,
                        kind="personal",
                        name="Personal",
                        is_default=True,
                    )
                )
            db.commit()

            async def seed():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(venue_handler)
                ) as client:
                    await capture_market_books(db, CID, PublicVenueCapture(client))

            asyncio.run(seed())
            db.commit()
        barrier = Barrier(2)

        def freeze(index):
            with Session(engine) as db:
                account = db.get(Account, index)
                barrier.wait(timeout=15)
                result = save_research(
                    db,
                    ResearchQuery(condition_id=CID, as_of=NOW, fee_bps="100", fee_version="test"),
                    account,
                )
                db.commit()
                return result

        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(freeze, [1, 2]))
        with Session(engine) as db:
            assert db.query(BookResearchRun).count() == 2
            assert db.query(ObservationDataset).count() == 1
            assert "fee_bps" not in db.query(ObservationDataset).one().manifest_json
            for result in results:
                assert replay_research(db, db.get(BookResearchRun, result["id"])) == result
        code = """
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Account
from app.services.market_data.book_research import ResearchQuery, save_research
from book_research_fixtures import CID, NOW
engine=create_engine(
 os.environ["TEST_DATABASE_URL"],
 connect_args={"options":"-csearch_path="+os.environ["BOOK_TEST_SCHEMA"]
               +" -cstatement_timeout=10000 -clock_timeout=10000"})
with Session(engine) as db:
 save_research(db, ResearchQuery(condition_id=CID, as_of=NOW), db.get(Account,1))
 os._exit(7)
"""
        env = {**os.environ, "BOOK_TEST_SCHEMA": schema, "PYTHONPATH": ".:tests:../services/shared"}
        result = subprocess.run(  # noqa: S603 - fixed test code, trusted interpreter, no shell
            [sys.executable, "-c", code], env=env, capture_output=True, timeout=120
        )
        assert result.returncode == 7
        with Session(engine) as db:
            assert db.query(BookResearchRun).count() == 2
        path = Path(__file__).parents[2] / "migrations/versions/20261027_0035_book_research.py"
        spec = importlib.util.spec_from_file_location("book_migration", path)
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
                revision.downgrade()
            assert "book_research_runs" not in inspect(connection).get_table_names()
            assert connection.execute(text("SELECT count(*) FROM users")).scalar() == 2
            assert connection.execute(text("SELECT count(*) FROM venue_observations")).scalar() == 3
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
    finally:
        engine.dispose()
        with admin.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
