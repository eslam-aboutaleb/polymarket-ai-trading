"""Disposable browser-test app. Only venue/AI boundaries are fixtures.

Run via playwright.research.config.ts; never imported by production. The real
analysis router, validation, normalization and browser transport run unchanged.
No wallet, signing key, production database, or live provider is needed.
"""

import asyncio
import json
from copy import deepcopy

from fastapi import FastAPI

from app.api.routes.analysis import analysis
from app.api.routes.auth.auth import get_optional_user_from_token
from app.security.rate_limit import limiter
from app.utils.database import get_db

CID = "0x" + "ab" * 32
revised_source = False


class FixtureVenue:
    async def get_market_outcome_tokens(self, condition_id):
        raw = {
            "condition_id": condition_id,
            "question": "Will the measure pass?",
            "description": (
                "Resolves YES if the official final tally passes the measure. A tie resolves NO."
            ),
            "active": True,
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
        if condition_id == "0x" + "16" * 32 and revised_source:
            raw["description"] += " Revised: certification must be final."
            raw["tokens"][1]["price"] = "0.8"
        return deepcopy(raw)


app = FastAPI()
app.state.limiter = limiter
limiter.enabled = False  # Deterministic parallel test traffic, not a production setting.
analysis.get_polymarket_service = FixtureVenue
app.include_router(analysis.router)
app.dependency_overrides[get_optional_user_from_token] = lambda: None
app.dependency_overrides[get_db] = lambda: None


class FixtureAgent:
    async def analyze_market(self, **kwargs):
        prompt = kwargs["market_description"]
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
            return {"analysis": "malformed output", "research_context": ""}
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
            "metadata": {"provider": "fixture", "model": "deterministic"},
        }

    async def close(self):
        pass


async def fixture_agent(user_id, db):
    return FixtureAgent(), None


analysis.get_analysis_client_for_user = fixture_agent


@app.post("/fixtures/revise-source")
def revise_source():
    global revised_source
    revised_source = True
    return {"revised": True}


@app.get("/")
def health():
    return {"status": "test fixture"}
