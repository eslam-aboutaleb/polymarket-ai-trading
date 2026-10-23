"""The native network boundary accepts one canonical ID, never a URL/path."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API, PolymarketService

CID = "0x" + "ab" * 32


@pytest.mark.parametrize(
    "identity",
    [
        None,
        123,
        "",
        "not-an-id",
        "https://attacker.example/",
        "//attacker.example/",
        CID + "/../admin",
        CID + "?redirect=https://attacker.example/",
        CID + "#fragment",
        CID + "\n",
        "0x" + "ab" * 31,
        "0x" + "ab" * 33,
        "0x" + "gg" * 32,
        "0x" + "ab" * 31 + "%2f",
        "../" + CID,
    ],
)
def test_invalid_condition_never_constructs_a_network_client(identity):
    with (
        patch("app.services.market_data.polymarket_service.httpx.AsyncClient") as factory,
        pytest.raises(ValueError, match="canonical 32-byte"),
    ):
        asyncio.run(PolymarketService().get_market_outcome_tokens(identity))
    factory.assert_not_called()


def test_valid_id_stays_on_native_origin_with_redirects_disabled():
    payload = {"condition_id": CID, "tokens": []}
    response = httpx.Response(200, json=payload, request=httpx.Request("GET", POLYMARKET_CLOB_API))
    with patch("app.services.market_data.polymarket_service.httpx.AsyncClient") as factory:
        client = factory.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=response)
        result = asyncio.run(PolymarketService().get_market_outcome_tokens("0x" + "AB" * 32))
    factory.assert_called_once_with(timeout=15.0, follow_redirects=False)
    client.get.assert_awaited_once_with(f"{POLYMARKET_CLOB_API}/markets/{CID}")
    assert result == payload
