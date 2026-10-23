"""Disposable browser-test app. Only venue/AI boundaries are fixtures.

Run via playwright.research.config.ts; never imported by production. The real
analysis router, validation, normalization and browser transport run unchanged.
No wallet, signing key, production database, or live provider is needed.
"""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.analysis import analysis
from app.api.routes.auth.auth import get_current_user_from_token, get_optional_user_from_token
from app.api.routes.trading import follower_replay, market_maker, replay_evidence
from app.config import get_settings
from app.models.analytics.follower_experiment import FollowerExperimentRun
from app.models.market_data.market_observation import MarketObservation
from app.models.market_data.replay_capture import ReplayCapture
from app.models.trading.market_maker_config import MarketMakerConfig
from app.models.trading.paper_maker_intent import PaperMakerIntent
from app.models.users.account import Account
from app.models.users.user import User
from app.security.rate_limit import limiter
from app.utils.database import get_db

CID = "0x" + "ab" * 32
revised_conditions: set[str] = set()


class FixtureVenue:
    async def get_market_outcome_tokens(self, condition_id):
        raw = {
            "condition_id": condition_id,
            "question": "Will the measure pass?",
            "description": (
                "Resolves YES if the official final tally passes the measure. A tie resolves NO."
            ),
            "active": True,
            "accepting_orders": True,
            "closed": False,
            "end_date_iso": "2026-11-03T23:59:00Z",
            "tokens": [
                {"outcome": "No", "token_id": "22", "price": "0.3"},
                {"outcome": "Yes", "token_id": "11", "price": "0.7"},
            ],
        }
        if condition_id == "0x" + "cd" * 32:
            raw.pop("description")
            raw["tokens"][0]["price"] = None
        if condition_id == "0x" + "ef" * 32:
            raw["condition_id"] = CID
        if condition_id in revised_conditions:
            raw["description"] += " Revised: certification must be final."
            raw["tokens"][1]["price"] = "0.8"
        return deepcopy(raw)


app = FastAPI()
app.state.limiter = limiter
limiter.enabled = False  # Deterministic parallel test traffic, not a production setting.
analysis.get_polymarket_service = FixtureVenue
app.include_router(analysis.router)
app.dependency_overrides[get_optional_user_from_token] = lambda: None
fixture_engine = create_engine(
    "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
)
for table in (
    User.__table__,
    Account.__table__,
    MarketObservation.__table__,
    ReplayCapture.__table__,
    FollowerExperimentRun.__table__,
    MarketMakerConfig.__table__,
    PaperMakerIntent.__table__,
):
    table.create(fixture_engine)
FixtureSession = sessionmaker(bind=fixture_engine)
with FixtureSession() as seed:
    seed.add(User(id=1, wallet_address="0x" + "11" * 20))
    seed.flush()
    seed.add(Account(id=1, owner_user_id=1, name="Personal", kind="personal", is_default=True))
    seed.add(
        MarketMakerConfig(
            id=1,
            user_id=1,
            account_id=1,
            condition_id="cond",
            token_id_yes="YES",
            token_id_no="NO",
            market_title="Synthetic maker fixture",
            enabled=True,
        )
    )
    seed.commit()
app.include_router(replay_evidence.router)
app.include_router(follower_replay.router)
app.include_router(market_maker.router)
app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
get_settings().follower_replay_enabled = True
get_settings().native_capture_enabled = True


def fixture_db():
    with FixtureSession() as session:
        yield session


app.dependency_overrides[get_db] = fixture_db


class FixtureAgent:
    async def analyze_market(self, **kwargs):
        prompt = kwargs.get("research_instructions") or kwargs["market_description"]
        if "0x" + "13" * 32 in prompt:
            raise RuntimeError("Fixture provider unavailable")
        if "0x" + "14" * 32 in prompt:
            await asyncio.sleep(10)
        condition_id = next(
            (
                "0x" + label * 32
                for label in ("11", "12", "14", "16")
                if "0x" + label * 32 in prompt
            ),
            CID,
        )
        if "0x" + "11" * 32 in prompt:
            return {
                "analysis": "malformed output",
                "research_context": "",
                "metadata": {"research_contract": "market-research-v1"},
            }
        quote = "Final certification may be delayed by a legal challenge."
        source = "https://evidence.example/report"
        draft = {
            "summary": "The official tally controls settlement, while certification needs review.",
            "claims": [
                {
                    "statement": "The tally determines settlement.",
                    "polarity": "supporting",
                    "source_url": f"https://clob.polymarket.com/markets/{condition_id}",
                    "quote": "Resolves YES if the official final tally passes the measure.",
                    "pivotal": True,
                },
                {
                    "statement": "Certification could be delayed.",
                    "polarity": "contradicting",
                    "source_url": source,
                    "quote": quote,
                    "pivotal": True,
                },
            ],
            "missing_information": ["Whether a legal challenge is still active."],
        }
        if "0x" + "12" * 32 in prompt:
            draft["claims"][1]["source_url"] = "javascript:alert(1)"
        return {
            "analysis": json.dumps(draft),
            "research_context": f"• Certification report\n  {quote}\n  Source: {source}",
            "metadata": {
                "provider": "fixture",
                "model": "deterministic",
                "research_contract": "market-research-v1",
            },
        }

    async def close(self):
        pass


async def fixture_agent(user_id, db):
    return FixtureAgent(), None


analysis.get_analysis_client_for_user = fixture_agent


@app.post("/fixtures/revise-source")
def revise_source(condition_id: str = "0x" + "16" * 32):
    revised_conditions.add(condition_id)
    return {"revised": True}


@app.get("/")
def health():
    return {"status": "test fixture"}


@app.post("/__fixtures/maker-cycle")
async def fixture_maker_cycle():
    from app.services.trading.market_maker_service import _sync_market_maker_paper

    with FixtureSession() as db:
        config = db.get(MarketMakerConfig, 1)
        return await _sync_market_maker_paper(
            db,
            config,
            0.5,
            [{"token_id": "YES", "side": "BUY", "price": 0.45, "size": 10}],
            {"best_bid": 0.49, "best_ask": 0.51, "depth": 1000},
        )


async def fixture_capture_venue():
    from app.services.market_data.replay_capture import PublicVenueCapture

    last_condition = CID

    async def respond(request):
        nonlocal last_condition
        now = datetime.now(UTC)
        if request.url.path.startswith("/markets/"):
            last_condition = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(
                200, json=await FixtureVenue().get_market_outcome_tokens(last_condition)
            )
        if request.url.path == "/markets":
            condition = request.url.params["condition_ids"]
            raw = await FixtureVenue().get_market_outcome_tokens(condition)
            return httpx.Response(
                200,
                json=[
                    {
                        "conditionId": condition,
                        "question": raw["question"],
                        "description": raw.get("description", ""),
                        "resolutionSource": "https://official.example/tally",
                        "active": True,
                        "closed": False,
                        "outcomes": '["No","Yes"]',
                        "clobTokenIds": '["22","11"]',
                    }
                ],
            )
        if request.url.path == "/activity":
            return httpx.Response(
                200,
                json=[
                    {
                        "conditionId": CID,
                        "asset": "11",
                        "proxyWallet": request.url.params["user"],
                        "type": "TRADE",
                        "side": "BUY",
                        "price": ".4",
                        "size": "25",
                        "timestamp": int(now.timestamp()) - 10,
                        "transactionHash": "0x" + "aa" * 32,
                    }
                ],
            )
        if request.url.path == "/book":
            return httpx.Response(
                200,
                json={
                    "market": last_condition,
                    "asset_id": request.url.params["token_id"],
                    "timestamp": str(int(now.timestamp() * 1000)),
                    "bids": [{"price": ".39", "size": "100"}],
                    "asks": [{"price": ".4", "size": "100"}],
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        yield PublicVenueCapture(client)


app.dependency_overrides[replay_evidence.get_capture_venue] = fixture_capture_venue
