"""Executable API + SQLite + controlled native HTTP, with private SDKs forbidden."""

from datetime import timedelta

import e2e_book_app as fixture
import httpx
from book_research_fixtures import NOW, raw_book
from py_clob_client_v2.utilities import (
    generate_orderbook_summary_hash,
    parse_raw_orderbook_summary,
)
from test_executable_replay import definition

from app.api.routes.trading.executable_replay import router
from app.config import get_settings
from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data import book_research
from app.services.market_data.replay_capture import PublicVenueCapture

app = fixture.app
app.include_router(router)
get_settings().executable_replay_enabled = True


def native(request):
    if not request.url.path.endswith("/book"):
        return fixture.transport(request)
    raw = raw_book(request.url.params["token_id"], fixture.clock[0])
    bid, ask = (
        ("0.45", "0.46") if fixture.clock[0] >= NOW + timedelta(seconds=4) else ("0.39", "0.40")
    )
    raw.update(bids=[{"price": bid, "size": "100"}], asks=[{"price": ask, "size": "100"}])
    raw["hash"] = generate_orderbook_summary_hash(parse_raw_orderbook_summary(raw))
    return httpx.Response(200, json=raw)


for sdk in (fixture.ClobClient, fixture.LegacyClobClient):
    for method in ("create_order", "post_order", "cancel", "cancel_all", "create_market_order"):
        if hasattr(sdk, method):
            setattr(sdk, method, fixture.forbidden)


@app.post("/__test/recipe")
async def recipe():
    await fixture.reset()
    with fixture.sessions() as db:
        for second in (1, 4):
            fixture.clock[0] = NOW + timedelta(seconds=second)
            async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
                await book_research.capture_market_books(
                    db, fixture.CID, PublicVenueCapture(client)
                )
        db.commit()
        model = definition().model_dump(mode="json")
        model["tape"]["specs"] = []
        model["tape"]["events"] = []
        for signal in model["tape"]["signals"]:
            signal.update(condition_id=fixture.CID, token_id="11")
        return {
            "definition": model,
            "capture_ids": [row.id for row in db.query(VenueObservation).all()],
        }
