"""Disposable actual archive/SQL/API server for browser regressions only."""

import atexit
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.observations import router
from app.config import get_settings
from app.models.market_data.venue_observation import (
    ObservationDataset,
    ObservationDatasetMember,
    VenueObservation,
)
from app.services.market_data.venue_observations import append_event
from app.utils.database import get_db

directory = TemporaryDirectory(prefix="observations-e2e-")
engine = create_engine(
    f"sqlite:///{directory.name}/archive.db", connect_args={"check_same_thread": False}
)
atexit.register(directory.cleanup)
atexit.register(engine.dispose)
sessions = sessionmaker(engine)
app = FastAPI()
app.include_router(router)
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}


def database():
    with sessions() as db:
        yield db


app.dependency_overrides[get_db] = database


def seed(db, sequence, receipt):
    return append_event(
        db,
        {
            "event_type": "book",
            "asset_id": "11",
            "sequence": sequence,
            "timestamp": str(int((receipt - timedelta(seconds=2)).timestamp() * 1000)),
            "bids": [{"price": "0.400000000000000001", "size": "25.123456789"}],
            "asks": [{"price": "0.6", "size": "30"}],
        },
        received_at=receipt,
        connection_id="fixture",
    )


@app.post("/__test/reset")
def reset():
    for model in (ObservationDatasetMember, ObservationDataset, VenueObservation):
        model.__table__.drop(engine, checkfirst=True)
    for model in (VenueObservation, ObservationDataset, ObservationDatasetMember):
        model.__table__.create(engine)
    get_settings().observation_archive_enabled = True
    now = datetime.now(UTC)
    with sessions.begin() as db:
        seed(db, 1, now - timedelta(seconds=30))
        seed(db, 3, now - timedelta(seconds=20))
        append_event(
            db,
            {
                "event_type": "source_status",
                "asset_id": "11",
                "status": "SOURCE_DISCONNECTED",
                "timestamp": str(int(now.timestamp() * 1000)),
            },
            received_at=now - timedelta(seconds=1),
            connection_id="fixture",
        )
        seed(db, 4, now + timedelta(hours=1))
    return {"as_of": now.isoformat()}


@app.post("/__test/disable")
def disable():
    get_settings().observation_archive_enabled = False
    return {"disabled": True}


@app.get("/")
def health():
    return {"status": "test"}
