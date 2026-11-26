"""Disposable fixture server for arbitrage UI e2e; never imported in production."""

import atexit
from tempfile import TemporaryDirectory

import httpx
from book_research_fixtures import CID, NOW, clob_market, raw_book
from fastapi import FastAPI
from py_clob_client_v2.utilities import generate_orderbook_summary_hash, parse_raw_orderbook_summary
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading.arbitrage import router
from app.config import get_settings
from app.models import Account, Base, User
from app.security.rate_limit import limiter
from app.services.analytics import arbitrage_service
from app.services.analytics.arbitrage import feasibility_service
from app.services.market_data import book_research, replay_capture
from app.services.market_data.replay_capture import PublicVenueCapture
from app.utils.database import get_db

folder = TemporaryDirectory(prefix="arbitrage-e2e-")
engine = create_engine(f"sqlite:///{folder.name}/arb.db", connect_args={"check_same_thread": False})
atexit.register(folder.cleanup)
atexit.register(engine.dispose)
sessions = sessionmaker(engine)
book_research.utc_now = lambda: NOW
replay_capture.utc_now = lambda: NOW
feasibility_service.utc_now = lambda: NOW
app = FastAPI()
app.include_router(router)
app.state.limiter = limiter
limiter.enabled = False
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
for flag in (
    "binary_book_research_enabled",
    "observation_archive_enabled",
    "native_capture_enabled",
    "arbitrage_feasibility_enabled",
):
    setattr(get_settings(), flag, True)


def database():
    with sessions() as db:
        yield db


app.dependency_overrides[get_db] = database
ASKS = {"11": [("0.45", "100")], "22": [("0.50", "100")]}


def transport(request):
    if request.url.path.endswith("/book"):
        token = request.url.params["token_id"]
        raw = raw_book(token, NOW)
        raw["asks"] = [{"price": p, "size": s} for p, s in ASKS[token]]
        raw["hash"] = ""
        raw["hash"] = generate_orderbook_summary_hash(parse_raw_orderbook_summary(raw))
        return httpx.Response(200, json=raw)
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


@app.get("/api/trades/arbitrage/opportunities")
def opportunities():
    return arbitrage_service.get_recent_opportunities()


@app.post("/__test/reset")
async def reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with sessions() as db:
        db.add(User(id=1, wallet_address="0x" + "11" * 20))
        db.flush()
        db.add(Account(id=1, owner_user_id=1, kind="personal", name="Personal", is_default=True))
        db.commit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await book_research.capture_market_books(db, CID, PublicVenueCapture(client))
        db.commit()
    arbitrage_service._arb_candidates.clear()
    market = {
        "condition_id": CID,
        "question": "Will the measure pass?",
        "outcomePrices": [0.45, 0.50],
    }
    wide = {
        "condition_id": "0x" + "ee" * 32,
        "question": "Wide book?",
        "bestBid": 0.40,
        "bestAsk": 0.60,
    }
    complement = arbitrage_service._check_complement_arbitrage(market)
    arbitrage_service.record_indicative_candidate(complement)
    arbitrage_service._arb_opportunities = [
        complement,
        arbitrage_service._check_spread_arbitrage(wide),
    ]
    return {"condition_id": CID}
