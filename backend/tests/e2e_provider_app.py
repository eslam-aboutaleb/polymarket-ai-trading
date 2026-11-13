"""Real API/PostgreSQL browser harness; only the outbound public HTTP boundary is controlled."""

import atexit
import json
import os
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.observations import router
from app.config import get_settings
from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data import provider_contract
from app.utils.database import get_db

url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not url or not url.startswith("postgresql"):
    raise RuntimeError("Provider browser tests require a disposable PostgreSQL database")
schema = "provider_browser_" + uuid4().hex
admin = create_engine(url)
with admin.begin() as connection:
    connection.execute(text(f'CREATE SCHEMA "{schema}"'))
engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
VenueObservation.__table__.create(engine)


def cleanup():
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


atexit.register(cleanup)
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:15137"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}


def database():
    with Session(engine) as db:
        yield db


app.dependency_overrides[get_db] = database
mode = "valid"
original_canary = provider_contract.run_provider_contract_canary


def provider(request):
    assert request.url.host == "clob.polymarket.com"
    assert request.url.path == "/prices-history"
    assert "authorization" not in request.headers
    if mode.isdecimal():
        return httpx.Response(int(mode), json={"error": "controlled provider outage"})
    if mode == "malformed":
        return httpx.Response(200, content=b'{"history": [')
    end = int(datetime.now(UTC).timestamp()) - 60
    rows = [{"t": end - (23 - i) * 3600, "p": 0.52} for i in range(24)]
    if mode == "gap":
        rows = rows[:10] + rows[15:]
    if mode == "empty":
        rows = []
    return httpx.Response(200, content=json.dumps({"history": rows}).encode())


async def controlled_http(token_id, *, enabled, lookback_hours=24):
    return await original_canary(
        token_id,
        enabled=enabled,
        lookback_hours=lookback_hours,
        transport=httpx.MockTransport(provider),
    )


provider_contract.run_provider_contract_canary = controlled_http


class Selection(BaseModel):
    mode: Literal["valid", "gap", "empty", "malformed", "401", "403", "429", "503", "disabled"]


@app.post("/__test/reset")
def reset(selection: Selection):
    global mode
    mode = selection.mode
    provider_contract._LAST_CANARY_MONO = None
    get_settings().historical_feed_canary_enabled = mode != "disabled"
    get_settings().observation_archive_enabled = True
    with Session(engine) as db:
        db.query(VenueObservation).delete()
        db.commit()
    return {"mode": mode}


@app.get("/")
def health():
    return {"status": "test", "database": "postgresql"}
