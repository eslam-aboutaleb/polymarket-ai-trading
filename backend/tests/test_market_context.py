"""Venue identity, source truthfulness and public API contract regressions."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.analysis.analysis import router
from app.security.rate_limit import limiter
from app.services.intelligence.market_context import MarketIdentityError, build_market_context
from app.utils.database import get_db

CID = "0x" + "ab" * 32
NOW = datetime(2026, 10, 22, tzinfo=UTC)
RAW = {
    "condition_id": CID,
    "question": "Will the measure pass?",
    "description": "Resolves YES if the official final tally passes the measure.",
    "end_date_iso": "2026-11-03T23:59:00Z",
    "active": True,
    "closed": False,
    # Deliberately reversed source order: token labels, not position, define sides.
    "tokens": [
        {"outcome": "No", "token_id": "22", "price": "0.3"},
        {"outcome": "Yes", "token_id": "11", "price": "0.7"},
    ],
}


def test_source_mapping_and_provenance_are_explicit():
    context = build_market_context(CID, RAW, NOW)
    assert [(o.outcome, o.token_id) for o in context.outcomes] == [("YES", "11"), ("NO", "22")]
    assert context.outcomes[0].indicative_price == 0.7
    assert context.rules.text == RAW["description"]
    assert context.rules.verification == "source_text_only"
    assert context.rules.settlement_cutoff is None
    assert context.rules.oracle is None
    assert context.quote_observed_at is None
    assert context.observed_at == NOW
    assert build_market_context(CID, deepcopy(RAW), NOW).snapshot_id == context.snapshot_id


@pytest.mark.parametrize("change", ["rules", "mapping", "date"])
def test_rule_identity_changes_when_interpretation_inputs_change(change):
    raw = deepcopy(RAW)
    if change == "rules":
        raw["description"] += " A tie resolves NO."
    elif change == "mapping":
        raw["tokens"][0]["token_id"], raw["tokens"][1]["token_id"] = "11", "22"
    else:
        raw["end_date_iso"] = "2027-01-01T00:00:00Z"
    assert (
        build_market_context(CID, raw, NOW).rules.content_hash
        != build_market_context(CID, RAW, NOW).rules.content_hash
    )


@pytest.mark.parametrize("price", [None, "NaN", "Infinity", "-0.1", "1.1", True, "not a price"])
def test_missing_or_invalid_prices_are_never_defaulted(price):
    raw = deepcopy(RAW)
    raw["tokens"][0]["price"] = price
    context = build_market_context(CID, raw, NOW)
    assert context.quote_quality == "unavailable"
    assert context.outcomes[1].indicative_price is None


@pytest.mark.parametrize("case", ["wrong_market", "nonbinary", "duplicate_side", "duplicate_token"])
def test_ambiguous_identity_is_rejected(case):
    raw = deepcopy(RAW)
    if case == "wrong_market":
        raw["condition_id"] = "0x" + "cd" * 32
    elif case == "nonbinary":
        raw["tokens"].append({"outcome": "Other", "token_id": "33"})
    elif case == "duplicate_side":
        raw["tokens"][0]["outcome"] = "Yes"
    else:
        raw["tokens"][0]["token_id"] = "11"
    with pytest.raises(MarketIdentityError):
        build_market_context(CID, raw, NOW)


def test_missing_rules_and_state_remain_unknown():
    raw = deepcopy(RAW)
    for key in ("description", "end_date_iso", "closed", "active"):
        raw.pop(key)
    context = build_market_context(CID, raw, NOW)
    assert context.rules.text == ""
    assert context.rules.market_end_date is None
    assert context.active is None and context.closed is None
    assert any("missing" in w for w in context.warnings)


@pytest.mark.parametrize("source_id", [None, 123, [], {}])
def test_malformed_source_identity_is_a_controlled_failure(source_id):
    with pytest.raises(MarketIdentityError):
        build_market_context(CID, {**RAW, "condition_id": source_id}, NOW)


@pytest.fixture
def api(market_observation_db):
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: market_observation_db
    limiter.reset()
    yield TestClient(app)
    limiter.reset()


def test_public_api_uses_only_requested_canonical_identity(api):
    service = AsyncMock()
    service.get_market_outcome_tokens.return_value = RAW
    with patch("app.api.routes.analysis.analysis.get_polymarket_service", return_value=service):
        response = api.get(f"/api/analysis/markets/{CID}/context?yes_price=0.01&end_date=forged")
    assert response.status_code == 200
    assert response.json()["outcomes"][0]["indicative_price"] == 0.7
    service.get_market_outcome_tokens.assert_awaited_once_with(CID)


def test_invalid_condition_id_never_reaches_provider(api):
    with patch("app.api.routes.analysis.analysis.get_polymarket_service") as factory:
        assert api.get("/api/analysis/markets/not-an-id/context").status_code == 422
    factory.assert_not_called()


@pytest.mark.parametrize("source_id", [None, 123])
def test_malformed_provider_identity_is_a_controlled_api_error(api, source_id):
    service = AsyncMock()
    service.get_market_outcome_tokens.return_value = {**RAW, "condition_id": source_id}
    with patch("app.api.routes.analysis.analysis.get_polymarket_service", return_value=service):
        response = api.get(f"/api/analysis/markets/{CID}/context")
    assert response.status_code == 422
    assert "different condition ID" in response.json()["detail"]


@pytest.mark.parametrize("upstream,expected", [(404, 404), (429, 503), (500, 503)])
def test_provider_errors_are_not_success_or_internal_details(api, upstream, expected):
    service = AsyncMock()
    request = httpx.Request("GET", "https://source.invalid")
    service.get_market_outcome_tokens.side_effect = httpx.HTTPStatusError(
        "private upstream diagnostic", request=request, response=httpx.Response(upstream)
    )
    with patch("app.api.routes.analysis.analysis.get_polymarket_service", return_value=service):
        response = api.get(f"/api/analysis/markets/{CID}/context")
    assert response.status_code == expected
    assert "private" not in response.text
