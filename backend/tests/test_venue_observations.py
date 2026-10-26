import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data.venue_observations import append_event, normalize


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    VenueObservation.__table__.create(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def book(**changes):
    return {
        "event_type": "book",
        "asset_id": "11",
        "market": "condition",
        "timestamp": "1760000000000",
        "sequence": 1,
        "bids": [{"price": "0.400000000000000001", "size": "25.123456789"}],
        "asks": [{"price": "0.5", "size": "30"}],
        **changes,
    }


def test_decimal_roundtrip_first_receipt_and_duplicates(db):
    now = datetime.now(UTC)
    first = append_event(db, book(), received_at=now, connection_id="one")
    db.commit()
    repeated = append_event(db, book(), received_at=now + timedelta(days=1), connection_id="two")
    assert repeated.id == first.id
    assert repeated.connection_id == "one"
    assert db.query(VenueObservation).count() == 1
    value = json.loads(first.normalized_json)
    assert Decimal(value["bids"][0]["price"]) == Decimal("0.400000000000000001")
    assert Decimal(value["bids"][0]["shares"]) == Decimal("25.123456789")


def test_missing_depth_and_price_ticks_are_not_books():
    value, flags = normalize(book(bids=[]))
    assert "INVALID_OR_MISSING_DEPTH" in flags and "bids" not in value
    value, flags = normalize({"event_type": "best_bid_ask", "best_bid": "0.4"})
    assert "INSUFFICIENT_DEPTH" in flags and "bids" not in value


def test_unknown_and_future_source_clocks_are_explicit(db):
    now = datetime.now(UTC)
    row = append_event(db, book(timestamp="bad"), received_at=now, connection_id="one")
    assert "SOURCE_CLOCK_UNKNOWN" in json.loads(row.quality_json)
    row = append_event(
        db,
        book(timestamp=str(int((now + timedelta(days=1)).timestamp() * 1000))),
        received_at=now,
        connection_id="one",
    )
    assert "SOURCE_CLOCK_AHEAD" in json.loads(row.quality_json)


def test_gap_reconnect_conflicting_sequence_and_late_events_remain_readable(db):
    now = datetime.now(UTC)
    first = append_event(db, book(sequence=1), received_at=now, connection_id="one")
    gap = append_event(
        db, book(sequence=3), received_at=now + timedelta(seconds=1), connection_id="two"
    )
    assert "SEQUENCE_GAP" in json.loads(gap.quality_json)
    assert "CONNECTION_CHANGED_GAP_POSSIBLE" in json.loads(gap.quality_json)
    late = append_event(
        db,
        book(sequence=3, timestamp="1750000000000"),
        received_at=now + timedelta(seconds=2),
        connection_id="two",
    )
    assert "SEQUENCE_REORDER_OR_CONFLICT" in json.loads(late.quality_json)
    assert "LATE_SOURCE_EVENT" in json.loads(late.quality_json)
    assert db.get(VenueObservation, first.id).content_hash == first.content_hash
    assert db.query(VenueObservation).count() == 3


def test_websocket_to_http_asof_roundtrip_and_auth(db, monkeypatch):
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.auth.auth import get_current_user_from_token
    from app.api.routes.markets.observations import router
    from app.config import get_settings
    from app.services.market_data import venue_observations as store
    from app.services.market_data.clob_ws_manager import ClobWsManager
    from app.utils.database import get_db

    monkeypatch.setattr(get_settings(), "observation_archive_enabled", True)
    received = []
    from sqlalchemy.orm import sessionmaker

    from app.utils import database

    db.commit()
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(db.get_bind()))
    real_persist = store.persist_message

    def persist(raw, at, connection):
        real_persist(raw, at, connection)
        received.append((raw, at, connection))

    monkeypatch.setattr(store, "persist_message", persist)
    manager = ClobWsManager()
    asyncio.run(manager._archive_message(json.dumps([book(), book(sequence=2)])))
    assert len(received) == 2
    db.expire_all()
    cutoff = received[0][1]
    future = append_event(
        db, book(sequence=4), received_at=cutoff + timedelta(hours=1), connection_id="later"
    )
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.get("/api/market-data/observations?token_id=11").status_code in (401, 403)
        app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
        response = client.get(
            "/api/market-data/observations", params={"token_id": "11", "as_of": cutoff.isoformat()}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["observations"]) == 2
        assert future.id not in [v["snapshot_id"] for v in data["observations"]]
        assert data["quality"] == "INSUFFICIENT_DATA"
        assert (
            client.get("/api/market-data/observations?token_id=11&as_of=2026-01-01").status_code
            == 422
        )
        monkeypatch.setattr(get_settings(), "observation_archive_enabled", False)
        assert client.get("/api/market-data/observations?token_id=11").status_code == 503


def test_archive_disabled_and_failed_capture_do_not_break_ticks(monkeypatch):
    import asyncio

    from app.config import get_settings
    from app.services.market_data import venue_observations as store
    from app.services.market_data.clob_ws_manager import ClobWsManager

    calls = []

    def fail(*args):
        calls.append(args)
        raise RuntimeError("disposable archive unavailable")

    monkeypatch.setattr(store, "persist_message", fail)
    manager = ClobWsManager()
    monkeypatch.setattr(get_settings(), "observation_archive_enabled", False)
    asyncio.run(manager._archive_message(json.dumps(book())))
    assert calls == []
    monkeypatch.setattr(get_settings(), "observation_archive_enabled", True)
    asyncio.run(manager._archive_message(json.dumps(book())))
    assert len(calls) == 1
    ticks = manager._extract_ticks(book())
    assert ticks[0]["token_id"] == "11"
