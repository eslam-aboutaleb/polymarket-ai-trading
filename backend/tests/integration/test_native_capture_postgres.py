"""Real PostgreSQL uniqueness and rollback for immutable first-receipt evidence."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models.market_data.replay_capture import ReplayCapture
from app.services.market_data.replay_capture import append_capture, aware, digest


@pytest.fixture
def sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for native capture PostgreSQL regressions")
    schema = "capture_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    ReplayCapture.__table__.create(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_concurrent_notifications_preserve_one_immutable_first_receipt(sessions):
    barrier = Barrier(2)
    now = datetime.now(UTC)
    identity = digest("same native signal")

    def write(index):
        with sessions() as db:
            # Both sessions see the ID absent before either tries the UNIQUE insert.
            event.listen(db, "before_flush", lambda *_: barrier.wait(timeout=15), once=True)
            row = append_capture(
                db,
                kind="signal",
                source_key=identity,
                raw={"receipt": index},
                observed_at=now + timedelta(seconds=index),
                stable_id=identity,
            )
            db.commit()
            return row.payload_json, aware(row.observed_at), row.content_hash

    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(write, range(2)))
    assert values[0] == values[1]
    with sessions() as db:
        assert db.query(ReplayCapture).count() == 1
        row = db.get(ReplayCapture, identity)
        assert row.content_hash == values[0][2]


def test_crash_rolls_back_enclosing_capture_transaction(sessions):
    now = datetime.now(UTC)
    identity = digest("rolled-back signal")
    with sessions() as db:
        append_capture(
            db, kind="gamma", source_key="native", raw={"rules": "captured"}, observed_at=now
        )
        append_capture(
            db,
            kind="signal",
            source_key=identity,
            raw={"trade": 1},
            observed_at=now,
            stable_id=identity,
        )
        db.rollback()
    with sessions() as restarted:
        assert restarted.query(ReplayCapture).count() == 0
