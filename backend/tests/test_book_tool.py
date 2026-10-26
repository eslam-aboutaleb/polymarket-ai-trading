import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import httpx
import pytest
from book_research_fixtures import CID
from polymarket_mcp import ResearchMCPClient


@pytest.fixture
def server(monkeypatch):
    if "ddgs" not in sys.modules:
        fake = ModuleType("ddgs")
        fake.DDGS = object
        monkeypatch.setitem(sys.modules, "ddgs", fake)
    path = Path(__file__).parents[2] / "services/mcps/research/server.py"
    spec = importlib.util.spec_from_file_location("book_research_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_book_tool_is_registered_get_only_bounded_and_no_raw_levels(server, monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "version": "binary-book-v1",
                "research_only": True,
                "status": "abstain",
                "reason": "BOOK_NOT_USABLE",
                "books": {"YES": {"bids": [], "asks": [], "grade": "STALE"}},
                "estimates": [{"curve": [], "status": "abstain"}],
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs),
    )
    result = server._run_one({"tool": "binary_book", "args": {"condition_id": CID}})
    assert result["ok"] and result["result"]["status"] == "abstain"
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.path == "/api/microstructure/book"
    assert "bids" not in result["result"]["books"]["YES"]
    assert server._run_one({"tool": "describe_book_research"})["result"]["read_only"]


def test_tool_provider_errors_remain_structured_abstentions(server, monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx,
        "AsyncClient",
        lambda **kwargs: real(
            transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs
        ),
    )
    result = asyncio.run(server.binary_book_research({"condition_id": CID}))
    assert result["status"] == "abstain" and result["version"] == "binary-book-v1"
    assert (
        asyncio.run(server.binary_book_research({"condition_id": "https://bad.invalid"}))["reason"]
        == "INVALID_CONDITION_ID"
    )


def test_shared_agent_client_invokes_registered_book_tool(monkeypatch):
    client = ResearchMCPClient()
    transport = AsyncMock(
        return_value={"version": "binary-book-v1", "status": "abstain", "reason": "NO_HISTORY"}
    )
    monkeypatch.setattr(client, "_call_tool", transport)
    result = asyncio.run(client.binary_book_research(CID, shares="10"))
    transport.assert_awaited_once_with("binary_book", {"condition_id": CID, "shares": "10"})
    assert result["reason"] == "NO_HISTORY"
