"""Native bytes → immutable evidence → saved, account-scoped follower replay."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.models.market_data.replay_capture import ReplayCapture
from app.services.analytics.replay.follower import FollowerExperiment, replay
from app.services.market_data.replay_capture import (
    PublicVenueCapture,
    capture_wallet,
    digest,
    frozen_dataset,
)

CID = "0x" + "ab" * 32
WALLET = "0x" + "a" * 40
NOW = datetime.now(UTC).replace(microsecond=0)


def venue_response(request):
    if request.url.path == "/activity":
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": CID,
                    "asset": "11",
                    "proxyWallet": WALLET,
                    "type": "TRADE",
                    "side": "BUY",
                    "size": 25,
                    "price": ".4",
                    "timestamp": int(NOW.timestamp()) - 5,
                    "transactionHash": "0x" + "a" * 64,
                }
            ],
        )
    if request.url.path == "/markets":
        return httpx.Response(
            200,
            json=[
                {
                    "conditionId": CID,
                    "question": "Test",
                    "description": "Rule",
                    "resolutionSource": "https://official.example/report",
                    "active": True,
                    "closed": False,
                    "outcomes": '["Yes","No"]',
                    "clobTokenIds": '["11","22"]',
                }
            ],
        )
    if request.url.path.startswith("/markets/"):
        return httpx.Response(
            200,
            json={
                "condition_id": CID,
                "question": "Test",
                "description": "Rule",
                "active": True,
                "closed": False,
                "accepting_orders": True,
                "tokens": [
                    {"token_id": "11", "outcome": "Yes", "price": ".4"},
                    {"token_id": "22", "outcome": "No", "price": ".6"},
                ],
            },
        )
    if request.url.path == "/book":
        return httpx.Response(
            200,
            json={
                "market": CID,
                "asset_id": "11",
                "timestamp": str(int(datetime.now(UTC).timestamp() * 1000)),
                "bids": [{"price": ".39", "size": "100"}],
                "asks": [{"price": ".4", "size": "100"}],
            },
        )
    return httpx.Response(404)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "native.sqlite"))
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def capture(db, handler=venue_response):
    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await capture_wallet(db, WALLET, PublicVenueCapture(client))

    return asyncio.run(call())


def frozen(db):
    return frozen_dataset(db, WALLET, NOW - timedelta(minutes=1), datetime.now(UTC))


def experiment(dataset, **changes):
    return FollowerExperiment(
        wallet=WALLET,
        start_at=NOW - timedelta(minutes=1),
        end_at=datetime.now(UTC),
        fee_bps=0,
        delays_seconds=[0],
        dataset=dataset,
        **changes,
    )


def test_native_capture_persists_first_receipt_and_provenance_across_sessions(sessions):
    with sessions() as db:
        first = capture(db)
        assert first["books_captured"] == 1 and first["failures"] == []
        assert first["historical_backfill"] is False
        signal = db.query(ReplayCapture).filter_by(kind="signal").one()
        identity, received = signal.id, signal.observed_at
        assert signal.source_at < received
    with sessions() as db:
        capture(db)
        signal = db.query(ReplayCapture).filter_by(kind="signal").one()
        assert (signal.id, signal.observed_at) == (identity, received)
        dataset, refs = frozen(db)
        assert len(dataset.signals) == 1 and len(dataset.books) == 2
        assert len(refs) == 7  # One signal, two books and two pairs of native metadata.
        assert {db.get(ReplayCapture, r).kind for r in refs} == {
            "signal",
            "book",
            "gamma",
            "clob_market",
        }
        assert all(
            digest(json.loads(db.get(ReplayCapture, r).payload_json))
            == db.get(ReplayCapture, r).content_hash
            for r in refs
        )
        value = replay(experiment(dataset))["scenarios"][0]["valuation"]
        assert Decimal(value["nav"]) == Decimal("999.75")
        early, _ = frozen_dataset(db, WALLET, NOW - timedelta(days=1), NOW - timedelta(minutes=1))
        assert early.signals == early.books == []


@pytest.mark.parametrize(
    "fault",
    [
        "gamma_condition",
        "gamma_tokens",
        "book_condition",
        "book_token",
        "future_book",
        "future_trade",
        "no_transaction",
        "bad_price",
        "bad_size",
        "closed",
        "unknown_lifecycle",
        "wrong_wallet",
        "missing_wallet",
        "non_trade",
        "gamma_closed",
        "orders_paused",
    ],
)
def test_invalid_native_evidence_never_becomes_a_fill(sessions, fault):
    def respond(request):
        response = venue_response(request)
        raw = response.json()
        if request.url.path == "/markets" and fault == "gamma_condition":
            raw[0]["conditionId"] = "0x" + "cd" * 32
        if request.url.path == "/markets" and fault == "gamma_tokens":
            raw[0]["clobTokenIds"] = '["22","11"]'
        if request.url.path.startswith("/markets/") and fault == "closed":
            raw["closed"] = True
        if request.url.path.startswith("/markets/") and fault == "unknown_lifecycle":
            raw.pop("active")
        if request.url.path == "/markets" and fault == "gamma_closed":
            raw[0]["closed"] = True
        if request.url.path.startswith("/markets/") and fault == "orders_paused":
            raw["accepting_orders"] = False
        if request.url.path == "/book":
            if fault == "book_condition":
                raw["market"] = "0x" + "cd" * 32
            if fault == "book_token":
                raw["asset_id"] = "22"
            if fault == "future_book":
                raw["timestamp"] = str(
                    int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000)
                )
        if request.url.path == "/activity":
            if fault == "wrong_wallet":
                raw[0]["proxyWallet"] = "0x" + "b" * 40
            if fault == "missing_wallet":
                raw[0].pop("proxyWallet")
            if fault == "non_trade":
                raw[0]["type"] = "REDEEM"
            if fault == "future_trade":
                raw[0]["timestamp"] = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
            if fault == "no_transaction":
                raw[0].pop("transactionHash")
            if fault == "bad_price":
                raw[0]["price"] = "NaN"
            if fault == "bad_size":
                raw[0]["size"] = -1
        return httpx.Response(200, json=raw)

    with sessions() as db:
        result = capture(db, respond)
        assert result["books_captured"] == 0 and result["failures"]
        dataset, _ = frozen(db)
        assert replay(experiment(dataset))["scenarios"][0]["valuation"]["nav"] == "1000"


def test_rule_change_after_last_signal_and_closed_market_reject_window(sessions):
    with sessions() as db:
        capture(db)

        def changed(request):
            response = venue_response(request)
            raw = response.json()
            if request.url.path == "/markets":
                raw[0]["description"] = "Changed rule"
            return httpx.Response(200, json=raw)

        capture(db, changed)
        with pytest.raises(ValueError, match="rule change"):
            frozen(db)


def test_repeated_native_depth_never_replenishes_from_new_clock_or_other_side(sessions):
    def respond(request):
        response = venue_response(request)
        raw = response.json()
        if request.url.path == "/activity":
            raw[0]["transactionHash"] = "0x" + respond.tx * 64
        if request.url.path == "/book":
            raw["asks"][0]["size"] = "25"
            raw["bids"][0]["price"] = respond.bid
        return httpx.Response(200, json=raw)

    respond.tx = "a"
    respond.bid = ".39"
    with sessions() as db:
        capture(db, respond)
        respond.tx = "b"
        respond.bid = ".38"
        capture(db, respond)
        dataset, _ = frozen(db)
        result = replay(experiment(dataset))["scenarios"][0]
        assert [row["shares"] for row in result["outcomes"]] == ["25", "0"]
        assert result["outcomes"][1]["reason"] == "insufficient_cash_or_inventory_or_depth"


def test_missing_source_clock_and_mutated_payload_fail_closed(sessions):
    with sessions() as db:
        capture(db)
        signal = db.query(ReplayCapture).filter_by(kind="signal").one()
        signal.source_at = None
        db.commit()
        with pytest.raises(ValueError, match="source clock"):
            frozen(db)
        signal.source_at = NOW
        db.commit()
        book = db.query(ReplayCapture).filter_by(kind="book").one()
        book.payload_json = "{}"
        db.commit()
        with pytest.raises(ValueError, match="integrity"):
            frozen(db)


def test_trade_before_follow_start_is_not_inherited_profit(sessions):
    with sessions() as db:
        capture(db)
        signal = db.query(ReplayCapture).filter_by(kind="signal").one()
        start = signal.observed_at.replace(tzinfo=UTC) - timedelta(microseconds=1)
        dataset, _ = frozen_dataset(db, WALLET, start, datetime.now(UTC))
        run = FollowerExperiment(
            wallet=WALLET,
            start_at=start,
            end_at=datetime.now(UTC),
            fee_bps=0,
            dataset=dataset,
            delays_seconds=[0],
        )
        assert replay(run)["scenarios"][0]["outcomes"] == []


@pytest.mark.parametrize(
    "identity",
    ["https://attacker.invalid/", "../", "0x" + "ab" * 32 + "?q=1", "0x" + "ab" * 32 + "\n"],
)
def test_market_boundary_rejects_url_injection_before_network(identity):
    calls = []

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: calls.append(r))
        ) as client:
            with pytest.raises(ValueError, match="identity"):
                await PublicVenueCapture(client).market(identity)

    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://attacker.invalid/"}),
        httpx.Response(200, content=b"x" * 1_000_001),
    ],
)
def test_native_response_redirect_and_size_bounds(response):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
            with pytest.raises(ValueError):
                await PublicVenueCapture(client).get("https://clob.polymarket.com/book")

    asyncio.run(run())


def test_signal_clock_is_activity_receipt_not_later_metadata_receipt(sessions, monkeypatch):
    start = datetime.now(UTC)
    times = iter(start + timedelta(seconds=n) for n in (0, 10, 20, 30))
    monkeypatch.setattr("app.services.market_data.replay_capture.utc_now", lambda: next(times))
    with sessions() as db:
        capture(db)
        signal = db.query(ReplayCapture).filter_by(kind="signal").one()
        market = db.query(ReplayCapture).filter_by(kind="clob_market").one()
        assert signal.observed_at.replace(tzinfo=UTC) == start
        assert market.observed_at > signal.observed_at


def test_closure_after_signal_rejects_window_even_when_clob_still_active(sessions):
    with sessions() as db:
        capture(db)

        def closed_gamma(request):
            raw = venue_response(request).json()
            if request.url.path == "/markets":
                raw[0]["closed"] = True
            return httpx.Response(200, json=raw)

        capture(db, closed_gamma)
        with pytest.raises(ValueError, match="closed or inactive"):
            frozen(db)
