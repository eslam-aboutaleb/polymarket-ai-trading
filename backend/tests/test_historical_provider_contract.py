"""Contract & operational acceptance tests for historical-feed provider (#54)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services.market_data.provider_contract as pc
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.main import app as fastapi_app
from app.models.market_data.venue_observation import VenueObservation
from app.services.market_data.provider_contract import (
    POLYMARKET_CLOB_PRICES_CONTRACT,
    ProviderContract,
    ProviderContractError,
    evaluate_provider_response,
    run_provider_contract_canary,
    validate_provider_contract,
)
from app.utils.database import get_db

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _valid_history_bytes(count: int = 24, *, now: datetime = NOW) -> bytes:
    end_ts = int(now.timestamp()) - 60
    start_ts = end_ts - (count - 1) * 3600
    rows = [{"t": start_ts + i * 3600, "p": 0.52} for i in range(count)]
    return json.dumps({"history": rows}).encode("utf-8")


@pytest.mark.parametrize(
    ("status", "expected_health", "expected_canary"),
    [
        (401, "UNAUTHORIZED", "failed"),
        (403, "UNAUTHORIZED", "failed"),
        (429, "RATE_LIMITED", "rate_limited"),
        (500, "UNAVAILABLE", "failed"),
        (503, "UNAVAILABLE", "failed"),
    ],
)
def test_provider_http_error_statuses_fail_closed(status, expected_health, expected_canary):
    raw = b'{"error":"upstream"}'
    report = evaluate_provider_response(
        POLYMARKET_CLOB_PRICES_CONTRACT,
        http_status=status,
        raw_body=raw,
        expected_bars=24,
        now=NOW,
    )
    assert report.health_state == expected_health
    assert report.canary_status == expected_canary
    assert report.raw_checksum_sha256 == hashlib.sha256(raw).hexdigest()
    assert report.credentials_used is False


@pytest.mark.parametrize(
    ("raw_body", "expected_reason"),
    [
        (b"{truncated", "TRUNCATED_OR_INVALID_JSON"),
        (b'{"history": []}', "EMPTY_HISTORY"),
        (b'{"history": [{"t": 100, "p": 0.5}], "next_cursor": "abc"}', "INCOMPLETE_PAGE_REJECTED"),
        (
            json.dumps({"history": [{"t": 200, "p": 0.5}, {"t": 100, "p": 0.5}]}).encode(),
            "OUT_OF_ORDER_TIMESTAMPS",
        ),
        (
            json.dumps({"history": [{"t": 100, "p": 0.5}, {"t": 100, "p": 0.6}]}).encode(),
            "DUPLICATE_TIMESTAMP",
        ),
        (
            json.dumps({"history": [{"t": 100, "p": 1.25}]}).encode(),
            "UNRECONCILED_PRICE_RANGE",
        ),
    ],
)
def test_provider_malformed_and_unreconciled_fixtures_degrade(raw_body, expected_reason):
    report = evaluate_provider_response(
        POLYMARKET_CLOB_PRICES_CONTRACT,
        http_status=200,
        raw_body=raw_body,
        expected_bars=24,
        now=NOW,
    )
    assert report.health_state == "DEGRADED"
    assert report.canary_status == "failed"
    assert report.failure_reason == expected_reason


def test_provider_200_valid_and_stale_coverage_reporting():
    raw = _valid_history_bytes(24, now=NOW)
    ok = evaluate_provider_response(
        POLYMARKET_CLOB_PRICES_CONTRACT,
        http_status=200,
        raw_body=raw,
        expected_bars=24,
        now=NOW,
    )
    assert ok.health_state == "READY"
    assert ok.canary_status == "passed"
    assert ok.observed_coverage == 1.0
    assert ok.missing_bars == 0
    assert ok.stale_delay_seconds == 60.0

    # Partial coverage (<80%) must degrade.
    partial = evaluate_provider_response(
        POLYMARKET_CLOB_PRICES_CONTRACT,
        http_status=200,
        raw_body=_valid_history_bytes(10, now=NOW),
        expected_bars=24,
        now=NOW,
    )
    assert partial.health_state == "DEGRADED"
    assert partial.missing_bars == 14
    assert partial.failure_reason == "STALE_OR_INSUFFICIENT_COVERAGE"


def test_prohibited_provider_and_silent_substitution_rejected():
    for bad_id in ("tradingview", "tradingview_mcp", "yahoo", "yfinance", "custom_unapproved"):
        bad = ProviderContract(
            **{**POLYMARKET_CLOB_PRICES_CONTRACT.model_dump(), "provider_id": bad_id}
        )
        with pytest.raises(ProviderContractError):
            validate_provider_contract(bad)


def test_canary_opt_in_rate_limit_and_mock_transport(monkeypatch):
    import asyncio

    monkeypatch.setattr(pc, "_LAST_CANARY_MONO", None)
    disabled = asyncio.run(run_provider_contract_canary("12345", enabled=False))
    assert disabled.canary_status == "disabled"
    assert disabled.health_state == "UNAVAILABLE"

    raw = _valid_history_bytes(24, now=datetime.now(UTC))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=raw))
    first = asyncio.run(run_provider_contract_canary("12345", enabled=True, transport=transport))
    assert first.canary_status == "passed"
    assert first.health_state == "READY"
    assert first.credentials_used is False

    # Immediate second call must hit local rate limit.
    second = asyncio.run(run_provider_contract_canary("12345", enabled=True, transport=transport))
    assert second.canary_status == "rate_limited"
    assert second.health_state == "RATE_LIMITED"
    assert second.failure_reason == "CANARY_LOCAL_RATE_LIMIT"


def test_provider_contract_api_endpoints(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    VenueObservation.__table__.create(engine)
    db = Session(engine)
    fastapi_app.dependency_overrides[get_db] = lambda: db
    fastapi_app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    client = TestClient(fastapi_app)
    get_settings.cache_clear()
    try:
        contract_resp = client.get("/api/market-data/provider-contract")
        assert contract_resp.status_code == 200
        assert contract_resp.json()["contract"]["provider_id"] == "polymarket_clob_prices_history"
        assert contract_resp.json()["canary_enabled"] is False

        canary_disabled = client.post("/api/market-data/provider-canary?token_id=12345")
        assert canary_disabled.status_code == 503

        monkeypatch.setenv("HISTORICAL_FEED_CANARY_ENABLED", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(pc, "_LAST_CANARY_MONO", None)
        raw = _valid_history_bytes(24, now=datetime.now(UTC))

        async def _fake_canary(token_id: str, *, enabled: bool, lookback_hours: int = 24):
            report = evaluate_provider_response(
                POLYMARKET_CLOB_PRICES_CONTRACT,
                http_status=200,
                raw_body=raw,
                expected_bars=lookback_hours,
                now=datetime.now(UTC),
            )
            report.raw_body_base64 = base64.b64encode(raw).decode("ascii")
            return report

        monkeypatch.setattr(pc, "run_provider_contract_canary", _fake_canary)
        canary_ok = client.post("/api/market-data/provider-canary?token_id=12345")
        assert canary_ok.status_code == 200
        assert canary_ok.json()["health_state"] == "READY"
        assert canary_ok.json()["credentials_used"] is False
    finally:
        fastapi_app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()


@pytest.mark.parametrize("count", [20, 23])
def test_any_missing_hour_fails_the_declared_complete_history_contract(count):
    report = evaluate_provider_response(
        POLYMARKET_CLOB_PRICES_CONTRACT,
        http_status=200,
        raw_body=_valid_history_bytes(count),
        expected_bars=24,
        now=NOW,
    )
    assert report.health_state == "DEGRADED"
    assert report.canary_status == "failed"
    assert report.missing_bars == 24 - count


def test_dense_burst_cannot_count_as_hourly_coverage():
    end = int(NOW.timestamp())
    raw = json.dumps({"history": [{"t": end - i, "p": 0.5} for i in range(24, 0, -1)]})
    report = evaluate_provider_response(
        POLYMARKET_CLOB_PRICES_CONTRACT, http_status=200, raw_body=raw.encode(), now=NOW
    )
    assert report.health_state == "DEGRADED"
    assert report.failure_reason == "CADENCE_MISMATCH"


def test_future_and_out_of_window_rows_do_not_prove_coverage():
    for seconds in [-48 * 3600, 1]:
        raw = json.dumps({"history": [{"t": int(NOW.timestamp()) + seconds, "p": 0.5}]})
        report = evaluate_provider_response(
            POLYMARKET_CLOB_PRICES_CONTRACT, http_status=200, raw_body=raw.encode(), now=NOW
        )
        assert report.failure_reason == "TIMESTAMP_OUTSIDE_REQUESTED_WINDOW"


def test_contract_identity_cannot_escalate_provider_permissions():
    contract = POLYMARKET_CLOB_PRICES_CONTRACT.model_copy(
        update={"permitted_use": "licensed_execution"}
    )
    with pytest.raises(ProviderContractError, match="CONTRACT_REVISION"):
        validate_provider_contract(contract)


def test_recorded_official_history_exposes_real_missing_hours():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "polymarket_clob_prices_history_yes.json").read_text()
    )
    now = datetime.fromisoformat(fixture["_recorded_at"].replace("Z", "+00:00"))
    report = evaluate_provider_response(
        POLYMARKET_CLOB_PRICES_CONTRACT,
        http_status=200,
        raw_body=json.dumps(fixture["prices_history"]).encode(),
        expected_bars=120,
        now=now,
    )
    assert report.canary_status == "failed"
    assert report.missing_bars > 0
    assert 0 < report.observed_coverage < 1


def test_keyless_standalone_canary_does_not_boot_application_security():
    code = """
import asyncio, httpx
from app.services.market_data.provider_contract import run_provider_contract_canary
async def probe():
    def provider(request):
        assert request.url.host == 'clob.polymarket.com'
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        return httpx.Response(503, json={'unavailable': True})
    report = await run_provider_contract_canary('12345', enabled=True,
        transport=httpx.MockTransport(provider))
    assert report.health_state == 'UNAVAILABLE'
    assert report.credentials_used is False
asyncio.run(probe())
"""
    subprocess.run(  # noqa: S603 - fixed Python executable and test source, isolated environment.
        [sys.executable, "-c", code],
        check=True,
        timeout=15,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[1])},
        capture_output=True,
    )


def test_transport_failure_and_oversized_response_cannot_report_readiness(monkeypatch):
    import asyncio

    def failed(_):
        raise httpx.ReadTimeout("controlled timeout")

    for handler in [failed, lambda _: httpx.Response(200, content=b"x" * 4_000_001)]:
        monkeypatch.setattr(pc, "_LAST_CANARY_MONO", None)
        report = asyncio.run(
            run_provider_contract_canary(
                "12345", enabled=True, transport=httpx.MockTransport(handler)
            )
        )
        assert report.health_state == "UNAVAILABLE"
        assert report.canary_status == "failed"
        assert report.raw_checksum_sha256 is None


def test_receipt_clock_is_distinct_from_requested_window(monkeypatch):
    import asyncio
    from datetime import timedelta

    moments = iter([NOW, NOW + timedelta(seconds=30)])
    monkeypatch.setattr(pc, "utc_now", lambda: next(moments))
    monkeypatch.setattr(pc, "_LAST_CANARY_MONO", None)
    report = asyncio.run(
        run_provider_contract_canary(
            "12345",
            enabled=True,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=_valid_history_bytes())
            ),
        )
    )
    assert report.checked_at == NOW + timedelta(seconds=30)
    assert report.requested_end_utc == NOW
    assert report.stale_delay_seconds == 90
