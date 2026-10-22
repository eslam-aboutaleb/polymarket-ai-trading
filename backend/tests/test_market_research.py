"""Typed brief validation, attribution, fail-closed decisions and SSE lifecycle."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_market_context import CID, NOW, RAW

from app.api.routes.analysis import analysis
from app.api.routes.auth.auth import get_optional_user_from_token
from app.security.rate_limit import limiter
from app.services.intelligence.market_context import build_market_context
from app.services.intelligence.market_research import finalize_research, safe_research_url
from app.utils.database import get_db

CONTEXT = build_market_context(CID, RAW, NOW)
SOURCE = "https://evidence.example/report"
EXCERPT = "Final certification may be delayed by a legal challenge."
RESEARCH = f"== Tavily Research ==\n• Certification update\n  {EXCERPT}\n  Source: {SOURCE}"
DRAFT = {
    "summary": "The official tally determines the outcome, but certification timing needs review.",
    "claims": [
        {
            "statement": "The tally controls the outcome.",
            "polarity": "supporting",
            "source_url": CONTEXT.source_url,
            "quote": RAW["description"],
            "pivotal": True,
        },
        {
            "statement": "Certification timing is uncertain.",
            "polarity": "contradicting",
            "source_url": SOURCE,
            "quote": EXCERPT,
            "pivotal": True,
        },
    ],
    "missing_information": ["Whether the legal challenge has concluded."],
}


def test_captured_excerpts_are_attributed_without_claiming_truth_or_trade_approval():
    result = finalize_research(CONTEXT, json.dumps(DRAFT), RESEARCH, {"provider": "test"})
    assert result.status == "investigate"
    assert [c.attribution for c in result.claims] == ["venue_excerpt", "search_excerpt"]
    assert result.execution_eligible is False
    assert result.provider == "test"
    assert result.context.snapshot_id == CONTEXT.snapshot_id
    assert any("truth is not verified" in m for m in result.missing_information)


@pytest.mark.parametrize("change", ["fake_quote", "wrong_url", "no_quote", "unsafe_url"])
def test_unsupported_pivotal_claim_cannot_become_a_ready_brief(change):
    draft = deepcopy(DRAFT)
    claim = draft["claims"][1]
    if change == "fake_quote":
        claim["quote"] = "The measure is guaranteed to pass."
    elif change == "wrong_url":
        claim["source_url"] = "https://other.example/unrelated"
    elif change == "no_quote":
        claim["quote"] = ""
    else:
        claim["source_url"] = "javascript:alert(1)"
    result = finalize_research(CONTEXT, json.dumps(draft), RESEARCH)
    assert result.status == "insufficient_evidence"
    assert "unsupported_pivotal_claim" in result.reason_codes
    assert result.claims[1].attribution == "unverified"
    assert result.claims[1].source_url is None


def test_quote_from_another_source_cannot_be_attributed_to_a_known_url():
    research = RESEARCH + "\n• Other report\n  Different content\n  Source: https://other.example"
    draft = deepcopy(DRAFT)
    draft["claims"][1]["source_url"] = "https://other.example"
    assert finalize_research(CONTEXT, json.dumps(draft), research).status == "insufficient_evidence"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not JSON",
        "[]",
        '{"summary":"BUY"}',
        json.dumps({**DRAFT, "status": "buy"}),
        json.dumps({**DRAFT, "claims": DRAFT["claims"] * 7}),
        "x" * 64001,
    ],
)
def test_malformed_extra_or_unbounded_output_fails_closed(raw):
    result = finalize_research(CONTEXT, raw, RESEARCH)
    assert result.status == "insufficient_evidence"
    assert result.claims == []
    assert "invalid_structured_output" in result.reason_codes


def test_no_pivotal_evidence_is_insufficient():
    draft = {**DRAFT, "claims": []}
    assert (
        "no_attributed_pivotal_evidence"
        in finalize_research(CONTEXT, json.dumps(draft), RESEARCH).reason_codes
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://localhost/a",
        "https://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://user:password@example.com",
        "https://api.internal/a",
        "http://example.com",
        "https://example.com:8000",
        "https://[::1]",
        "https://127.1/report",
        "https://foo.localhost./report",
        "https://0x7f000001/report",
    ],
)
def test_unsafe_model_links_are_not_allowed(url):
    assert not safe_research_url(url)


def test_unstructured_web_context_does_not_prove_attribution():
    assert finalize_research(CONTEXT, json.dumps(DRAFT), f"{SOURCE} {EXCERPT}").status == (
        "insufficient_evidence"
    )


@pytest.fixture
def api(market_observation_db):
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(analysis.router)
    app.dependency_overrides[get_optional_user_from_token] = lambda: {"user_id": 7}
    app.dependency_overrides[get_db] = lambda: market_observation_db
    limiter.reset()
    yield TestClient(app)
    limiter.reset()


def test_stream_uses_canonical_context_user_provider_and_one_terminal_success(api):
    service = AsyncMock()
    service.get_market_outcome_tokens.return_value = RAW
    client = AsyncMock()
    client.analyze_market.return_value = {
        "analysis": json.dumps(DRAFT),
        "research_context": RESEARCH,
        "metadata": {"research_contract": "market-research-v1"},
    }
    with (
        patch.object(analysis, "get_polymarket_service", return_value=service),
        patch.object(
            analysis,
            "get_analysis_client_for_user",
            AsyncMock(return_value=(client, {"model": "chosen"})),
        ) as factory,
    ):
        response = api.post("/api/analysis/market/research/stream", json={"condition_id": CID})
    frames = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [f.get("type") for f in frames] == [
        "context_ready",
        "research_started",
        "analysis_completed",
        None,
    ]
    assert frames[-1] == {"done": True}
    assert frames[2]["result"]["status"] == "investigate"
    assert frames[1]["transport"] == "unary"
    assert factory.call_args.args[0] == 7
    assert client.analyze_market.call_args.kwargs["yes_price"] == 0.7
    assert client.analyze_market.call_args.kwargs["market_description"] == RAW["description"]
    assert "JSON" in client.analyze_market.call_args.kwargs["research_instructions"]
    assert client.analyze_market.call_args.kwargs["llm_config"] == {"model": "chosen"}
    client.close.assert_awaited_once()


def test_stream_error_has_no_success_terminal_and_closes_provider(api):
    service = AsyncMock()
    service.get_market_outcome_tokens.return_value = RAW
    client = AsyncMock()
    client.analyze_market.side_effect = RuntimeError("secret diagnostic")
    with (
        patch.object(analysis, "get_polymarket_service", return_value=service),
        patch.object(
            analysis, "get_analysis_client_for_user", AsyncMock(return_value=(client, None))
        ),
    ):
        response = api.post("/api/analysis/market/research/stream", json={"condition_id": CID})
    assert '"error"' in response.text and '"done"' not in response.text
    assert "secret diagnostic" not in response.text
    client.close.assert_awaited_once()


def test_caller_cannot_override_prices_or_rules(api):
    with patch.object(analysis, "get_polymarket_service") as factory:
        response = api.post(
            "/api/analysis/market/research/stream",
            json={
                "condition_id": CID,
                "yes_price": 0.01,
                "market_description": "Forged",
            },
        )
    assert response.status_code == 422
    factory.assert_not_called()


def test_missing_inputs_skip_provider_without_blame_on_generation(api):
    service = AsyncMock()
    service.get_market_outcome_tokens.return_value = {**RAW, "description": ""}
    with (
        patch.object(analysis, "get_polymarket_service", return_value=service),
        patch.object(analysis, "get_analysis_client_for_user", AsyncMock()) as factory,
    ):
        response = api.post("/api/analysis/market/research/stream", json={"condition_id": CID})
    frames = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    result = next(frame["result"] for frame in frames if frame.get("type") == "analysis_completed")
    assert result["reason_codes"] == ["missing_market_inputs"]
    assert "not generated" in result["summary"]
    factory.assert_not_awaited()


def test_cancelled_stream_cancels_provider_and_closes_client(market_observation_db):
    async def run():
        service = AsyncMock()
        service.get_market_outcome_tokens.return_value = RAW
        client = AsyncMock()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def generate(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        client.analyze_market.side_effect = generate
        with (
            patch.object(analysis, "get_polymarket_service", return_value=service),
            patch.object(
                analysis, "get_analysis_client_for_user", AsyncMock(return_value=(client, None))
            ),
        ):
            response = await analysis.market_research_stream(
                analysis.MarketResearchRequest(condition_id=CID),
                current_user=None,
                db=market_observation_db,
            )
            iterator = response.body_iterator
            await anext(iterator)  # context_ready
            await anext(iterator)  # research_started
            pending = asyncio.create_task(anext(iterator))
            await asyncio.wait_for(started.wait(), timeout=2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert cancelled.is_set()
            client.close.assert_awaited_once()

    asyncio.run(run())


def test_old_provider_cannot_silently_fall_back_to_legacy_contract(api):
    source = AsyncMock()
    source.get_market_outcome_tokens.return_value = RAW
    client = AsyncMock()
    client.analyze_market.return_value = {
        "analysis": json.dumps(DRAFT),
        "research_context": RESEARCH,
        "metadata": {},
    }
    with (
        patch.object(analysis, "get_polymarket_service", return_value=source),
        patch.object(
            analysis, "get_analysis_client_for_user", AsyncMock(return_value=(client, None))
        ),
    ):
        response = api.post("/api/analysis/market/research/stream", json={"condition_id": CID})
    assert '"error"' in response.text
    assert '"analysis_completed"' not in response.text
    assert '"done"' not in response.text
    client.close.assert_awaited_once()
