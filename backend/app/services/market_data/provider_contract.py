"""Licensed historical-feed provider contract and opt-in canary (#54).

Enforces explicit provider provenance, raw-response SHA-256 checksums,
coverage/missing-bar/staleness telemetry, and fail-closed readiness states.
Never sends trading credentials and forbids silent provider substitution
(e.g. Yahoo or TradingView).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.utils.time import utc_now

ProviderHealthState = Literal["READY", "DEGRADED", "RATE_LIMITED", "UNAVAILABLE", "UNAUTHORIZED"]
CanaryStatus = Literal["passed", "failed", "rate_limited", "disabled"]

PROHIBITED_PROVIDERS = frozenset({"tradingview", "tradingview_mcp", "yahoo", "yfinance"})
OFFICIAL_CLOB_API = "https://clob.polymarket.com"


class ProviderContractError(ValueError):
    """Raised when a provider contract is prohibited or substituted."""


class ProviderContract(BaseModel):
    """Explicit contract for a historical market-data provider."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    source_symbol: str
    event_time_field: str
    receipt_time_field: str
    data_granularity: str
    revision_policy: Literal["immutable", "append_only", "revisable"]
    permitted_use: Literal["research_indicative_only", "licensed_execution", "prohibited"]
    history_window_days: int = Field(gt=0)
    timestamp_semantics: Literal["utc_epoch_seconds", "iso8601_utc"]
    rate_limit_rpm: int = Field(gt=0, le=600)
    incomplete_page_policy: Literal["fail_closed", "reject_partial"]
    normalization_version: str


POLYMARKET_CLOB_PRICES_CONTRACT = ProviderContract(
    provider_id="polymarket_clob_prices_history",
    source_symbol="clob_token_id",
    event_time_field="t",
    receipt_time_field="available_at",
    data_granularity="60m",
    revision_policy="append_only",
    permitted_use="research_indicative_only",
    history_window_days=30,
    timestamp_semantics="utc_epoch_seconds",
    rate_limit_rpm=12,
    incomplete_page_policy="fail_closed",
    normalization_version="prices_history_feed_v1",
)


class ProviderCanaryReport(BaseModel):
    """Operational readiness & coverage report for a historical-feed provider."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str
    source_symbol: str
    normalization_version: str
    permitted_use: str
    health_state: ProviderHealthState
    canary_status: CanaryStatus
    http_status: int | None = None
    raw_checksum_sha256: str | None = None
    observed_bars: int = 0
    expected_bars: int = 0
    observed_coverage: float = 0.0
    missing_bars: int = 0
    stale_delay_seconds: float | None = None
    failure_reason: str | None = None
    checked_at: datetime
    requested_start_utc: datetime | None = None
    requested_end_utc: datetime | None = None
    credentials_used: Literal[False] = False
    capture_id: str | None = None
    raw_body_base64: str | None = Field(default=None, exclude=True, repr=False)


def validate_provider_contract(contract: ProviderContract) -> None:
    """Reject prohibited or unlicensed providers (no silent substitution)."""
    pid = contract.provider_id.strip().lower()
    if pid in PROHIBITED_PROVIDERS or contract.permitted_use == "prohibited":
        raise ProviderContractError(f"PROHIBITED_PROVIDER:{contract.provider_id}")
    if pid != POLYMARKET_CLOB_PRICES_CONTRACT.provider_id:
        raise ProviderContractError(f"UNAPPROVED_PROVIDER_SUBSTITUTION:{contract.provider_id}")
    if contract != POLYMARKET_CLOB_PRICES_CONTRACT:
        raise ProviderContractError("UNAPPROVED_PROVIDER_CONTRACT_REVISION")


def evaluate_provider_response(
    contract: ProviderContract,
    *,
    http_status: int,
    raw_body: bytes,
    expected_bars: int = 24,
    now: datetime | None = None,
    max_stale_seconds: int = 4 * 3600,
    window_end: datetime | None = None,
) -> ProviderCanaryReport:
    """Evaluate a recorded or live provider HTTP response against the contract."""
    validate_provider_contract(contract)
    if not 1 <= expected_bars <= 168 or not 0 <= max_stale_seconds <= 86400:
        raise ValueError("Bounded positive hourly window and staleness limit required")
    if now is not None and now.tzinfo is None:
        raise ValueError("Timezone-aware receipt time required")
    if len(raw_body) > 4_000_000:
        raise ValueError("Provider response exceeds bounded contract payload")
    checked_at = now.astimezone(UTC) if now and now.tzinfo else (now or utc_now())
    window_end = window_end or checked_at
    if window_end.tzinfo is None or window_end > checked_at:
        raise ValueError("Requested window must be timezone-aware and available at receipt")
    checksum = hashlib.sha256(raw_body).hexdigest()

    base = {
        "provider_id": contract.provider_id,
        "source_symbol": contract.source_symbol,
        "normalization_version": contract.normalization_version,
        "permitted_use": contract.permitted_use,
        "http_status": http_status,
        "raw_checksum_sha256": checksum,
        "expected_bars": expected_bars,
        "missing_bars": expected_bars,
        "checked_at": checked_at,
        "requested_start_utc": window_end - timedelta(hours=expected_bars),
        "requested_end_utc": window_end,
    }

    if http_status in (401, 403):
        return ProviderCanaryReport(
            **base,
            health_state="UNAUTHORIZED",
            canary_status="failed",
            failure_reason=f"HTTP_{http_status}_UNAUTHORIZED",
        )
    if http_status == 429:
        return ProviderCanaryReport(
            **base,
            health_state="RATE_LIMITED",
            canary_status="rate_limited",
            failure_reason="HTTP_429_RATE_LIMITED",
        )
    if http_status >= 500 or http_status != 200:
        return ProviderCanaryReport(
            **base,
            health_state="UNAVAILABLE",
            canary_status="failed",
            failure_reason=f"HTTP_{http_status}_UNAVAILABLE",
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ProviderCanaryReport(
            **base,
            health_state="DEGRADED",
            canary_status="failed",
            failure_reason="TRUNCATED_OR_INVALID_JSON",
        )

    if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
        return ProviderCanaryReport(
            **base,
            health_state="DEGRADED",
            canary_status="failed",
            failure_reason="INVALID_PAYLOAD_SHAPE",
        )

    if payload.get("next_cursor") or payload.get("truncated") is True:
        return ProviderCanaryReport(
            **base,
            health_state="DEGRADED",
            canary_status="failed",
            failure_reason="INCOMPLETE_PAGE_REJECTED",
        )

    history: list[Any] = payload["history"]
    if not history:
        return ProviderCanaryReport(
            **base,
            health_state="DEGRADED",
            canary_status="failed",
            failure_reason="EMPTY_HISTORY",
        )

    if len(history) > 10_000:
        return ProviderCanaryReport(
            **base, health_state="DEGRADED", canary_status="failed", failure_reason="TOO_MANY_ROWS"
        )
    seen_ts: set[int] = set()
    prev_ts: int | None = None
    for row in history:
        if not isinstance(row, dict):
            return ProviderCanaryReport(
                **base,
                health_state="DEGRADED",
                canary_status="failed",
                failure_reason="MALFORMED_ROW",
            )
        raw_t = row.get(contract.event_time_field)
        raw_p = row.get("p")
        if isinstance(raw_t, bool) or not isinstance(raw_t, int):
            return ProviderCanaryReport(
                **base,
                health_state="DEGRADED",
                canary_status="failed",
                failure_reason="INVALID_TIMESTAMP",
            )
        if raw_t in seen_ts:
            return ProviderCanaryReport(
                **base,
                health_state="DEGRADED",
                canary_status="failed",
                failure_reason="DUPLICATE_TIMESTAMP",
            )
        if prev_ts is not None and raw_t < prev_ts:
            return ProviderCanaryReport(
                **base,
                health_state="DEGRADED",
                canary_status="failed",
                failure_reason="OUT_OF_ORDER_TIMESTAMPS",
            )
        try:
            price = Decimal(str(raw_p))
        except (InvalidOperation, ValueError, TypeError):
            return ProviderCanaryReport(
                **base,
                health_state="DEGRADED",
                canary_status="failed",
                failure_reason="INVALID_PRICE",
            )
        if not price.is_finite() or price < 0 or price > 1:
            return ProviderCanaryReport(
                **base,
                health_state="DEGRADED",
                canary_status="failed",
                failure_reason="UNRECONCILED_PRICE_RANGE",
            )
        seen_ts.add(raw_t)
        prev_ts = raw_t

    end_ts = int(window_end.timestamp())
    start_ts = end_ts - expected_bars * 3600
    if any(t < start_ts or t > end_ts for t in seen_ts):
        return ProviderCanaryReport(
            **base,
            health_state="DEGRADED",
            canary_status="failed",
            failure_reason="TIMESTAMP_OUTSIDE_REQUESTED_WINDOW",
        )
    times = sorted(seen_ts)
    # Official hourly samples have bounded publication jitter, not exact wall-clock alignment.
    # Count distinct requested hourly slots and reject a burst masquerading as hourly history.
    if any(b - a < 3540 for a, b in zip(times, times[1:], strict=False)):
        return ProviderCanaryReport(
            **base,
            health_state="DEGRADED",
            canary_status="failed",
            failure_reason="CADENCE_MISMATCH",
        )
    slots = {(end_ts - t) // 3600 for t in times}
    observed_bars = len(slots)
    missing_bars = max(0, expected_bars - observed_bars)
    coverage = min(1.0, observed_bars / expected_bars) if expected_bars > 0 else 1.0
    latest_dt = datetime.fromtimestamp(prev_ts, tz=UTC)
    stale_delay = max(0.0, (checked_at - latest_dt).total_seconds())

    base.update(
        observed_bars=observed_bars,
        observed_coverage=round(coverage, 4),
        missing_bars=missing_bars,
        stale_delay_seconds=round(stale_delay, 2),
    )
    inadequate = stale_delay > max_stale_seconds or missing_bars > 0
    return ProviderCanaryReport(
        **base,
        health_state="DEGRADED" if inadequate else "READY",
        canary_status="failed" if inadequate else "passed",
        failure_reason="STALE_OR_INSUFFICIENT_COVERAGE" if inadequate else None,
    )


_LAST_CANARY_MONO: float | None = None


async def run_provider_contract_canary(
    token_id: str,
    *,
    enabled: bool,
    contract: ProviderContract = POLYMARKET_CLOB_PRICES_CONTRACT,
    lookback_hours: int = 24,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderCanaryReport:
    """Run an opt-in, rate-limited, credential-free canary probe against CLOB."""
    global _LAST_CANARY_MONO
    validate_provider_contract(contract)
    if not isinstance(token_id, str) or not re.fullmatch(r"[0-9]{1,120}", token_id):
        raise ValueError("Explicit decimal token identity required")
    if not 1 <= lookback_hours <= 168:
        raise ValueError("Bounded positive lookback required")
    now = utc_now()

    if not enabled:
        return ProviderCanaryReport(
            provider_id=contract.provider_id,
            source_symbol=token_id,
            normalization_version=contract.normalization_version,
            permitted_use=contract.permitted_use,
            health_state="UNAVAILABLE",
            canary_status="disabled",
            expected_bars=lookback_hours,
            missing_bars=lookback_hours,
            failure_reason="CANARY_DISABLED",
            checked_at=now,
        )

    min_interval = 60.0 / contract.rate_limit_rpm
    mono_now = time.monotonic()
    if _LAST_CANARY_MONO is not None and (mono_now - _LAST_CANARY_MONO) < min_interval:
        return ProviderCanaryReport(
            provider_id=contract.provider_id,
            source_symbol=token_id,
            normalization_version=contract.normalization_version,
            permitted_use=contract.permitted_use,
            health_state="RATE_LIMITED",
            canary_status="rate_limited",
            expected_bars=lookback_hours,
            missing_bars=lookback_hours,
            failure_reason="CANARY_LOCAL_RATE_LIMIT",
            checked_at=now,
        )
    _LAST_CANARY_MONO = mono_now

    end_ts = int(now.timestamp())
    start_ts = end_ts - lookback_hours * 3600
    try:
        async with (
            asyncio.timeout(15),
            httpx.AsyncClient(timeout=10.0, transport=transport) as client,
            client.stream(
                "GET",
                f"{OFFICIAL_CLOB_API}/prices-history",
                params={"market": token_id, "startTs": start_ts, "endTs": end_ts, "fidelity": 60},
                headers={"Accept": "application/json"},
                follow_redirects=False,
            ) as resp,
        ):
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                if len(body) + len(chunk) > 4_000_000:
                    raise httpx.HTTPError("Provider payload exceeded contract bound")
                body.extend(chunk)
        raw_body = bytes(body)
        report = evaluate_provider_response(
            contract,
            http_status=resp.status_code,
            raw_body=raw_body,
            expected_bars=lookback_hours,
            now=utc_now(),
            window_end=now,
        )
        report.source_symbol = token_id
        report.raw_body_base64 = base64.b64encode(raw_body).decode("ascii")
        return report
    except (httpx.HTTPError, TimeoutError):
        return ProviderCanaryReport(
            provider_id=contract.provider_id,
            source_symbol=token_id,
            normalization_version=contract.normalization_version,
            permitted_use=contract.permitted_use,
            health_state="UNAVAILABLE",
            canary_status="failed",
            expected_bars=lookback_hours,
            missing_bars=lookback_hours,
            failure_reason="NETWORK_ERROR",
            checked_at=now,
        )


def record_provider_probe(db, token_id: str, report: ProviderCanaryReport) -> str:
    """Retain the public response and its contract in the existing immutable receipt archive."""
    from app.services.market_data.venue_observations import REST_SOURCE, append_event

    if report.raw_body_base64 is not None:
        raw = base64.b64decode(report.raw_body_base64, validate=True)
        if hashlib.sha256(raw).hexdigest() != report.raw_checksum_sha256:
            raise ValueError("Provider raw-response integrity mismatch")
    elif report.raw_checksum_sha256 is not None:
        raise ValueError("A response checksum requires retained public response bytes")

    row = append_event(
        db,
        {
            "event_type": "provider_canary",
            "asset_id": token_id,
            "endpoint": f"{OFFICIAL_CLOB_API}/prices-history",
            "timestamp": str(int(report.checked_at.timestamp() * 1000)),
            "contract": POLYMARKET_CLOB_PRICES_CONTRACT.model_dump(mode="json"),
            "report": report.model_dump(mode="json", exclude={"capture_id"}),
            "raw_body_base64": report.raw_body_base64,
        },
        received_at=report.checked_at,
        connection_id="provider-contract-v1",
        source=REST_SOURCE,
    )
    return row.id
