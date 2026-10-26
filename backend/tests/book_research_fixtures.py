"""Native HTTP fixtures with explicit metadata, size rules and server hashes."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.models import Account, Base, User
from app.services.market_data import book_research, replay_capture
from app.services.market_data.replay_capture import PublicVenueCapture

CID = "0x" + "ab" * 32
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def clob_market():
    return {
        "condition_id": CID,
        "question": "Will the measure pass?",
        "description": "Final official tally.",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "neg_risk": False,
        "minimum_tick_size": "0.01",
        "minimum_order_size": "1",
        "tokens": [{"token_id": "11", "outcome": "Yes"}, {"token_id": "22", "outcome": "No"}],
    }


def raw_book(token, now=NOW):
    from py_clob_client_v2.utilities import (
        generate_orderbook_summary_hash,
        parse_raw_orderbook_summary,
    )

    raw = {
        "market": CID,
        "asset_id": token,
        "timestamp": str(int(now.timestamp() * 1000)),
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.60", "size": "20"}, {"price": "0.50", "size": "10"}],
        "tick_size": "0.01",
        "min_order_size": "1",
        "neg_risk": False,
        "last_trade_price": "0.45",
        "hash": "",
    }
    raw["hash"] = generate_orderbook_summary_hash(parse_raw_orderbook_summary(raw))
    return raw


def venue_handler(request):
    if request.url.path.endswith("/book"):
        return httpx.Response(200, json=raw_book(request.url.params["token_id"]))
    if "gamma" in request.url.host:
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": CID,
                    "active": True,
                    "closed": False,
                    "enableOrderBook": True,
                    "outcomes": ["Yes", "No"],
                    "clobTokenIds": ["11", "22"],
                }
            ],
        )
    return httpx.Response(200, json=clob_market())


@pytest.fixture
def research_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(get_settings(), "binary_book_research_enabled", True)
    monkeypatch.setattr(get_settings(), "native_capture_enabled", True)
    monkeypatch.setattr(get_settings(), "observation_archive_enabled", True)
    monkeypatch.setattr(replay_capture, "utc_now", lambda: NOW)
    monkeypatch.setattr(book_research, "utc_now", lambda: NOW)
    with Session(engine) as db:
        db.add_all(
            [
                User(id=1, wallet_address="0x" + "11" * 20),
                User(id=2, wallet_address="0x" + "22" * 20),
            ]
        )
        db.flush()
        db.add_all(
            [
                Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True),
                Account(id=2, owner_user_id=1, name="Sub", kind="sub", is_default=False),
                Account(id=3, owner_user_id=2, name="Foreign", kind="personal", is_default=True),
            ]
        )
        db.commit()

        async def capture():
            async with httpx.AsyncClient(transport=httpx.MockTransport(venue_handler)) as client:
                return await book_research.capture_market_books(db, CID, PublicVenueCapture(client))

        result = asyncio.run(capture())
        assert result["status"] == "captured"
        db.commit()
        yield db
    engine.dispose()
