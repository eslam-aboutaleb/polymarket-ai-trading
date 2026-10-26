import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.market_data.venue_observation import (
    ObservationDataset,
    ObservationDatasetMember,
    VenueObservation,
)
from app.services.market_data.venue_observations import append_event, normalize


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for model in (VenueObservation, ObservationDataset, ObservationDatasetMember):
        model.__table__.create(engine)
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


def test_frozen_manifest_replay_survives_late_data_and_retention(db):
    from app.services.market_data.venue_observations import (
        freeze_dataset,
        prune_unpinned,
        replay_dataset,
    )

    now = datetime.now(UTC)
    row = append_event(db, book(), received_at=now, connection_id="one")
    frozen = freeze_dataset(db, "11", now)
    db.commit()
    baseline = replay_dataset(db, frozen["dataset_id"])
    assert freeze_dataset(db, "11", now) == frozen
    later = append_event(
        db,
        book(sequence=2, timestamp="1"),
        received_at=now + timedelta(seconds=1),
        connection_id="two",
    )
    db.commit()
    assert replay_dataset(db, frozen["dataset_id"]) == baseline
    assert prune_unpinned(db, now + timedelta(days=1)) == 1
    db.commit()
    assert db.get(VenueObservation, later.id) is None
    assert replay_dataset(db, frozen["dataset_id"]) == baseline
    row.normalized_json = "{}"
    db.flush()
    with pytest.raises(ValueError, match="integrity"):
        replay_dataset(db, frozen["dataset_id"])


def test_snapshot_id_and_dataset_api_integrity(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes.auth.auth import get_current_user_from_token
    from app.api.routes.markets.observations import router
    from app.config import get_settings
    from app.utils.database import get_db

    monkeypatch.setattr(get_settings(), "observation_archive_enabled", True)
    now = datetime.now(UTC)
    row = append_event(db, book(), received_at=now, connection_id="one")
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.get(f"/api/market-data/observations/{row.id}").json()["raw"] == book()
        assert (
            client.get(
                f"/api/market-data/observations/{row.id}",
                params={"as_of": (now - timedelta(seconds=1)).isoformat()},
            ).status_code
            == 404
        )
        frozen = client.post(
            "/api/market-data/datasets", params={"token_id": "11", "as_of": now.isoformat()}
        ).json()
        replay = client.get("/api/market-data/datasets/" + frozen["dataset_id"])
        assert replay.status_code == 200
        assert replay.json()["observations"][0]["snapshot_id"] == row.id
        row.payload_json = "{}"
        db.commit()
        assert client.get("/api/market-data/datasets/" + frozen["dataset_id"]).status_code == 409


def test_crossed_book_and_source_health_are_explicit(db):
    now = datetime.now(UTC)
    row = append_event(
        db, book(asks=[{"price": "0.3", "size": "1"}]), received_at=now, connection_id="one"
    )
    assert "CROSSED_BOOK" in json.loads(row.quality_json)
    row = append_event(
        db,
        {"event_type": "source_status", "status": "SOURCE_DISCONNECTED", "asset_id": "11"},
        received_at=now,
        connection_id="one",
    )
    assert json.loads(row.normalized_json)["status"] == "SOURCE_DISCONNECTED"


def test_cursor_pages_receipt_ties_and_cannot_cross_token_or_cutoff(db):
    from app.services.market_data.venue_observations import history

    now = datetime.now(UTC)
    ids = set()
    for sequence in range(4):
        ids.add(append_event(db, book(sequence=sequence), received_at=now, connection_id="one").id)
    page1 = history(db, "11", now, 2)
    page2 = history(db, "11", now, 2, before=page1[-1].id)
    assert {row.id for row in page1 + page2} == ids
    assert not {row.id for row in page1} & {row.id for row in page2}
    with pytest.raises(ValueError, match="cursor"):
        history(db, "22", now, before=page1[-1].id)
    with pytest.raises(ValueError, match="cursor"):
        history(db, "11", now - timedelta(seconds=1), before=page1[-1].id)


def test_outer_rollback_does_not_commit_a_receipt_or_manifest(db):
    from app.services.market_data.venue_observations import freeze_dataset

    now = datetime.now(UTC)
    append_event(db, book(), received_at=now, connection_id="one")
    freeze_dataset(db, "11", now)
    db.rollback()
    assert db.query(VenueObservation).count() == 0
    assert db.query(ObservationDataset).count() == 0


@pytest.mark.parametrize(
    "price,size",
    [
        ("NaN", "1"),
        ("Infinity", "1"),
        ("-0.1", "1"),
        ("1.1", "1"),
        ("0.4", "-1"),
        ("0.4", "NaN"),
    ],
)
def test_adversarial_depth_never_becomes_a_valid_book(price, size):
    value, flags = normalize(book(bids=[{"price": price, "size": size}]))
    assert "INVALID_OR_MISSING_DEPTH" in flags
    assert "bids" not in value


def test_same_fixture_has_stable_snapshot_ids_and_backfill_cannot_change_cutoff(db):
    from app.services.market_data.venue_observations import history

    now = datetime.now(UTC)
    baseline = append_event(db, book(), received_at=now, connection_id="one")
    db.commit()
    historical_ids = [row.id for row in history(db, "11", now)]
    for _ in range(10):
        assert (
            append_event(db, book(), received_at=now + timedelta(days=1), connection_id="retry").id
            == baseline.id
        )
    append_event(
        db,
        book(timestamp="1", sequence=2),
        received_at=now + timedelta(days=1),
        connection_id="backfill",
    )
    assert [row.id for row in history(db, "11", now)] == historical_ids
    assert len(history(db, "11", now + timedelta(days=1))) == 2


def test_archive_write_failure_is_reported_after_recovery(monkeypatch):
    import asyncio

    from app.config import get_settings
    from app.services.market_data import venue_observations as store
    from app.services.market_data.clob_ws_manager import ClobWsManager

    monkeypatch.setattr(get_settings(), "observation_archive_enabled", True)
    manager = ClobWsManager()
    asyncio.run(manager.subscribe(["11"]))
    received = []
    fail = [True]

    def persist(raw, *_):
        if fail[0]:
            raise RuntimeError("test write failure")
        received.append(raw)

    monkeypatch.setattr(store, "persist_message", persist)
    asyncio.run(manager._archive_message(json.dumps(book())))
    fail[0] = False
    asyncio.run(manager._archive_message(json.dumps(book(sequence=2))))
    assert received[-1]["status"] == "ARCHIVE_RECOVERED_WITH_GAP"


def test_archive_token_configuration_is_bounded():
    from app.config import Settings

    assert Settings(observation_archive_tokens="11,22").observation_archive_tokens == "11,22"
    for value in ("11,", "abc", "-1", "1," * 50 + "1"):
        with pytest.raises(ValueError):
            Settings(observation_archive_tokens=value)


def test_wire_numeric_depth_keeps_decimal_lexemes(monkeypatch):
    import asyncio

    from app.config import get_settings
    from app.services.market_data import venue_observations as store
    from app.services.market_data.clob_ws_manager import ClobWsManager

    monkeypatch.setattr(get_settings(), "observation_archive_enabled", True)
    received = []
    monkeypatch.setattr(store, "persist_message", lambda event, *_: received.append(event))
    wire = (
        '{"event_type":"book","asset_id":"11",'
        '"bids":[{"price":0.400000000000000001,"size":25.123456789}],'
        '"asks":[{"price":0.6,"size":1}]}'
    )
    asyncio.run(ClobWsManager()._archive_message(wire))
    value, _ = normalize(received[0])
    assert value["bids"][0] == {"price": "0.400000000000000001", "shares": "25.123456789"}
