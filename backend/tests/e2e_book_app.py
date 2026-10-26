"""Disposable native-provider/SQL/HTTP fixture server, never imported in production."""

import atexit
from datetime import timedelta
from tempfile import TemporaryDirectory

import httpx
from book_research_fixtures import CID, NOW, clob_market, raw_book
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets.microstructure import router
from app.config import get_settings
from app.models import Account, Base, User
from app.security.rate_limit import limiter
from app.services.market_data import book_research, replay_capture
from app.services.market_data.replay_capture import PublicVenueCapture, get_capture_venue
from app.services.market_data.venue_observations import append_event
from app.utils.database import get_db

folder = TemporaryDirectory(prefix="binary-book-e2e-")
engine = create_engine(
    f"sqlite:///{folder.name}/research.db", connect_args={"check_same_thread": False}
)
atexit.register(folder.cleanup)
atexit.register(engine.dispose)
sessions = sessionmaker(engine)
clock = [NOW]
halted = [False]
book_research.utc_now = lambda: clock[0]
replay_capture.utc_now = lambda: clock[0]
app = FastAPI()
app.include_router(router)
app.state.limiter = limiter
limiter.enabled = False
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
get_settings().binary_book_research_enabled = True
get_settings().observation_archive_enabled = True
get_settings().native_capture_enabled = True


def database():
    with sessions() as db:
        yield db


app.dependency_overrides[get_db] = database


def transport(request):
    if request.url.path.endswith("/book"):
        return httpx.Response(200, json=raw_book(request.url.params["token_id"], clock[0]))
    if "gamma" in request.url.host:
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": CID,
                    "active": not halted[0],
                    "closed": halted[0],
                    "enableOrderBook": True,
                    "outcomes": ["Yes", "No"],
                    "clobTokenIds": ["11", "22"],
                }
            ],
        )
    return httpx.Response(200, json={**clob_market(), "active": not halted[0], "closed": halted[0]})


async def venue():
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        yield PublicVenueCapture(client)


app.dependency_overrides[get_capture_venue] = venue


def forbidden(*args, **kwargs):
    raise AssertionError("book research accessed private execution")


from py_clob_client_v2.client import ClobClient  # noqa: E402

ClobClient.create_order = forbidden
ClobClient.post_order = forbidden
from py_clob_client.client import ClobClient as LegacyClobClient  # noqa: E402

LegacyClobClient.create_order = forbidden
LegacyClobClient.post_order = forbidden


@app.post("/__test/reset")
async def reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    clock[0], halted[0] = NOW, False
    with sessions() as db:
        db.add(User(id=1, wallet_address="0x" + "11" * 20))
        db.flush()
        db.add(Account(id=1, owner_user_id=1, kind="personal", name="Personal", is_default=True))
        db.commit()
        async for provider in venue():
            await book_research.capture_market_books(db, CID, provider)
        db.commit()
    return {"condition_id": CID}


@app.post("/__test/gap")
def gap():
    clock[0] += timedelta(seconds=1)
    with sessions.begin() as db:
        append_event(
            db,
            {
                "event_type": "price_change",
                "asset_id": "11",
                "market": CID,
                "timestamp": str(int(clock[0].timestamp() * 1000)),
                "price_changes": [{"asset_id": "11", "side": "SELL", "price": "0.5", "size": "0"}],
            },
            received_at=clock[0],
            connection_id="fixture",
        )
    clock[0] += timedelta(seconds=1)
    return {"gap": True}


@app.post("/__test/stale")
def stale():
    clock[0] += timedelta(seconds=61)
    return {"stale": True}


@app.get("/")
def health():
    return {"status": "test"}


@app.post("/__test/execution")
def recorded_execution():
    from app.models import LiveOrder, UserTrade

    with sessions.begin() as db:
        trade = UserTrade(
            user_id=1,
            account_id=1,
            market_id=CID,
            token_id="11",
            action="buy",
            amount=2.6,
            price=0.52,
            filled_size=5,
            filled_price=0.52,
            status="executed",
            order_hash="controlled-execution",
            executed_at=NOW + timedelta(seconds=2),
        )
        db.add(trade)
        db.flush()
        db.add(
            LiveOrder(
                user_id=1,
                account_id=1,
                trade_id=trade.id,
                idempotency_key="controlled-fixture",
                fingerprint="f" * 64,
                strategy="manual",
                token_id="11",
                side="BUY",
                price="0.52",
                shares=5,
                notional="2.6",
                reserved="2.6",
                state="FILLED",
                order_hash="controlled-execution",
                response={"settled": True, "size_matched": "5", "filled_notional": 2.6},
                created_at=NOW + timedelta(seconds=1),
            )
        )
        return {"trade_id": trade.id}
