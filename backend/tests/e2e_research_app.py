"""Disposable browser-test app. Only venue/AI boundaries are fixtures.

Run via playwright.research.config.ts; never imported by production. The real
analysis router, validation, normalization and browser transport run unchanged.
No wallet, signing key, production database, or live provider is needed.
"""

from copy import deepcopy

from fastapi import FastAPI

from app.api.routes.analysis import analysis
from app.security.rate_limit import limiter

CID = "0x" + "ab" * 32


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
        return deepcopy(raw)


app = FastAPI()
app.state.limiter = limiter
limiter.enabled = False  # Deterministic parallel test traffic, not a production setting.
analysis.get_polymarket_service = FixtureVenue
app.include_router(analysis.router)


@app.get("/")
def health():
    return {"status": "test fixture"}
