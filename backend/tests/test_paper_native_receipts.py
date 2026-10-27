"""Real native HTTP parsing → frozen receipt → journal → final payout."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from paper_account_fixtures import db as _paper_db
from paper_account_fixtures import order
from test_native_capture import venue_response

from app.models import PaperAccount, PaperJournalEvent, UserTrade
from app.services.analytics import paper_accounting as paper
from app.services.market_data.replay_capture import PublicVenueCapture, capture_market

CID = "0x" + "ab" * 32
db = _paper_db


def test_public_receipts_drive_entry_and_final_settlement_without_wallet_activity(db):
    session, clock = db
    closed = [False]
    paths = []

    def transport(request):
        paths.append(request.url.path)
        raw = venue_response(request).json()
        if request.url.path == "/book":
            raw.update(
                asset_id=request.url.params["token_id"], tick_size="0.01", min_order_size="0.01"
            )
        elif request.url.path == "/markets" and closed[0]:
            raw[0].update(
                active=False, closed=True, umaResolutionStatus="resolved", outcomePrices=["1", "0"]
            )
        elif request.url.path.startswith("/markets/") and closed[0]:
            raw.update(active=False, closed=True, accepting_orders=False)
            for token in raw["tokens"]:
                token["winner"] = token["token_id"] == "11"
        return httpx.Response(200, json=raw)

    async def capture():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await capture_market(session, CID, PublicVenueCapture(client))

    first = asyncio.run(capture())
    assert first["active"]
    clock[0] = datetime.now(UTC)
    result = order(session)
    assert result["status"] == "filled" and Decimal(result["fill_price"]) == Decimal(".4")
    closed[0] = True
    final = asyncio.run(capture())
    assert not final["active"]
    clock[0] = datetime.now(UTC)
    assert paper.settle_captured(session, 1, 1) == 1
    value = paper.value_account(session, session.get(PaperAccount, 1))
    assert Decimal(value["nav"]) == 1015
    assert "/activity" not in paths and paths.count("/book") == 2


def test_native_quotes_without_size_rules_cannot_prove_a_fill(db):
    session, clock = db

    def transport(request):
        raw = venue_response(request).json()
        if request.url.path == "/book":
            raw.update(asset_id=request.url.params["token_id"])
        return httpx.Response(200, json=raw)

    async def capture():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await capture_market(session, CID, PublicVenueCapture(client))

    asyncio.run(capture())
    clock[0] = datetime.now(UTC)
    assert order(session)["reason"] == "VENUE_SIZE_RULES_UNKNOWN"
    assert session.query(PaperJournalEvent).count() == session.query(UserTrade).count() == 0
