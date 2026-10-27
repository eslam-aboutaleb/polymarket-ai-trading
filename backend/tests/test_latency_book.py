import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from book_research_fixtures import CID
from latency_book_fixtures import candidate, snapshot

from app.services.trading import latency_book


def test_standard_objects_downsize_dollars_vs_shares():
    q = candidate(asks=[{"price": ".500", "size": "60"}])
    assert q.status == "READY"
    assert q.shares == 60 and q.worst_case_cash == 30 and q.authorized_cash == 50


@pytest.mark.parametrize("price", [".1", ".5", ".9"])
@pytest.mark.parametrize("cash", ["1", "10", "50", "99.99999999"])
def test_cash_and_precision_boundaries(price, cash):
    raw, meta, now = snapshot(
        asks=[{"price": price, "size": "1000"}],
        bids=[{"price": str(Decimal(price) - Decimal(".001")), "size": ".01"}],
    )
    q = latency_book.authorize(raw, meta, "11", cash, {"base_fee": "0"}, now, now, 0.02)
    assert q == latency_book.authorize(raw, meta, "11", cash, {"base_fee": "0"}, now, now, 0.02)
    assert q.status == "READY" and q.shares * q.limit_price <= Decimal(cash)
    assert q.shares % Decimal(".01") == 0


def test_multilevel_worst_case_cash_and_vwap():
    q = candidate(asks=[{"price": ".501", "size": "200"}, {"price": ".500", "size": "40"}])
    assert q.status == "READY" and q.limit_price == Decimal(".501")
    assert q.shares == Decimal("99.80") and q.vwap < q.limit_price and q.worst_case_cash <= 50


@pytest.mark.parametrize(
    "changes",
    [
        {"asks": []},
        {"bids": None},
        {"asks": [[0.5, 100]]},
        {"asks": [{"price": True, "size": "100"}]},
        {"asks": [{"price": ".5", "size": True}]},
        {"asks": [{"price": "NaN", "size": "100"}]},
        {"asks": [{"price": "Infinity", "size": "100"}]},
        {"asks": [{"price": "0", "size": "100"}]},
        {"asks": [{"price": ".5", "size": "-1"}]},
        {"asks": [{"price": ".5001", "size": "100"}]},
        {"asks": [{"price": ".5", "size": "1"}, {"price": ".5", "size": "2"}]},
        {"bids": [{"price": ".5", "size": "100"}]},
        {"asset_id": "22"},
        {"market": "wrong"},
        {"timestamp": None},
        {"timestamp": True},
        {"hash": "spoofed"},
    ],
)
def test_invalid_books_never_authorize(changes):
    assert candidate(**changes).status != "READY"


@pytest.mark.parametrize(
    "fee",
    [None, {}, {"base_fee": None}, {"base_fee": True}, {"base_fee": "1000"}, {"base_fee": "-1"}],
)
def test_unknown_fees_abstain(fee):
    raw, meta, now = snapshot()
    q = latency_book.authorize(raw, meta, "11", 50, fee, now, now, 0.02)
    assert q.status == "UNKNOWN_COST"


def test_stale_future_and_unknown_status():
    raw, meta, now = snapshot()
    assert (
        latency_book.authorize(
            raw, meta, "11", 50, {"base_fee": 0}, now, now + timedelta(seconds=2), 0.02
        ).status
        == "STALE"
    )
    raw["timestamp"] = str(int((now + timedelta(seconds=1)).timestamp() * 1000))
    assert (
        latency_book.authorize(raw, meta, "11", 50, {"base_fee": 0}, now, now, 0.02).status
        == "STALE"
    )
    for key in ["active", "accepting_orders"]:
        raw, meta, now = snapshot()
        del meta["raw"]["clob"][key]
        assert (
            latency_book.authorize(raw, meta, "11", 50, {"base_fee": 0}, now, now, 0.02).status
            != "READY"
        )


@pytest.mark.parametrize(
    "failure,reason",
    [
        (httpx.ReadTimeout("test"), "PROVIDER_TIMEOUT"),
        (httpx.ConnectError("test"), "PROVIDER_CONNECTION_FAILED"),
        (latency_book.EvidenceError("PROVIDER_HTTP_429"), "PROVIDER_HTTP_429"),
        (latency_book.EvidenceError("PROVIDER_HTTP_503"), "PROVIDER_HTTP_503"),
        (ValueError("secret provider data"), "MALFORMED_BOOK_OR_METADATA"),
    ],
)
def test_fetch_failure_codes(failure, reason):
    with patch.object(latency_book.LiveVenue, "market", AsyncMock(side_effect=failure)):
        q = asyncio.run(latency_book.fetch_executable_book(CID, "11", 50, 0.02))
    assert q.status != "READY" and q.reason == reason and "secret" not in q.model_dump_json()


@pytest.mark.parametrize("body", [b"null", b"[]", b"{", b"{}"])
def test_native_provider_bad_body_rejects(body):
    client_cls = httpx.AsyncClient
    with patch.object(
        latency_book.httpx,
        "AsyncClient",
        lambda: client_cls(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ),
    ):
        q = asyncio.run(latency_book.fetch_executable_book(CID, "11", 50, 0.02))
    assert q.status != "READY"


def test_fetch_provider_shapes_through_shared_adapter():
    raw, meta, _ = snapshot()

    def handler(request):
        if request.url.path.endswith("/book"):
            return httpx.Response(200, json=raw)
        if request.url.path.endswith("/fee-rate"):
            return httpx.Response(200, json={"base_fee": 0})
        if "gamma" in request.url.host:
            return httpx.Response(200, json=[meta["raw"]["gamma"]])
        return httpx.Response(200, json=meta["raw"]["clob"])

    client_cls = httpx.AsyncClient
    with patch.object(
        latency_book.httpx,
        "AsyncClient",
        lambda: client_cls(transport=httpx.MockTransport(handler)),
    ):
        q = asyncio.run(latency_book.fetch_executable_book(CID, "11", 50, 0.02))
    assert q.status == "READY", q


def test_explicit_up_down_mapping_preserves_raw_receipts():
    raw, meta, now = snapshot()
    meta["raw"]["clob"]["tokens"] = [
        {"token_id": "22", "outcome": "Down"},
        {"token_id": "11", "outcome": "Up"},
    ]
    meta["raw"]["gamma"]["outcomes"] = ["Up", "Down"]
    q = latency_book.authorize(raw, meta, "11", 50, {"base_fee": 0}, now, now, 0.02)
    assert q.status == "READY"
    assert meta["raw"]["clob"]["tokens"][0]["outcome"] == "Down"
    meta["raw"]["gamma"]["clobTokenIds"] = ["22", "11"]
    assert (
        latency_book.authorize(raw, meta, "11", 50, {"base_fee": 0}, now, now, 0.02).status
        != "READY"
    )


@pytest.mark.parametrize("body", [None, [], {}, "truncated"])
def test_malformed_book_body_returns_auditable_reason(body):
    _, meta, now = snapshot()
    q = latency_book.authorize(body, meta, "11", 50, {"base_fee": 0}, now, now, 0.02)
    assert q.status == "INVALID_BOOK" and q.received_at == now
