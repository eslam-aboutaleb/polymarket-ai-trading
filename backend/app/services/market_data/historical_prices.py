"""Validated, read-only Polymarket CLOB token price history for research backtests.

CLOB /prices-history accepts an outcome *token ID*, not a condition ID.
These observations are indicative price samples, not executable bid/ask quotes.
No missing price is ever fabricated and any upstream failure fails the run closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API

logger = logging.getLogger(__name__)
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_ID = re.compile(r"^[0-9]{1,80}$")
_CHUNK_DAYS = 30
_MAX_REQUESTS = 48
_FIDELITY_MINUTES = 60


class PriceHistoryError(ValueError):
    """Expected data-quality/provider error: never silently becomes a zero-return run."""


class HistoricalPriceRows(list):
    """Price rows plus immutable dataset provenance for the backtest result."""

    def __init__(self, rows: list[dict[str, Any]], provenance: dict[str, Any]):
        super().__init__(rows)
        self.provenance = provenance


def _window(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    except (ValueError, TypeError) as exc:
        raise PriceHistoryError("Invalid history date; use YYYY-MM-DD") from exc
    if start >= end:
        raise PriceHistoryError("History start date must precede end date")
    if end > datetime.now(UTC) + timedelta(days=1):
        raise PriceHistoryError("History end date cannot be in the future")
    if math.ceil((end - start).total_seconds() / (_CHUNK_DAYS * 86400)) > _MAX_REQUESTS:
        raise PriceHistoryError("Historical date range exceeds the safe request limit")
    return start, end


def _parse_market_tokens(payload: Any, condition_id: str, outcome: str) -> str:
    if not isinstance(payload, dict):
        raise PriceHistoryError("CLOB market response must be a JSON object")
    identity = payload.get("condition_id") or payload.get("conditionId")
    if not isinstance(identity, str) or identity.lower() != condition_id.lower():
        raise PriceHistoryError("Market lookup returned a different or unknown condition ID")
    tokens = payload.get("tokens")
    if not isinstance(tokens, list):
        raise PriceHistoryError("Market lookup has no outcome token mapping")
    matches = [
        t
        for t in tokens
        if isinstance(t, dict) and str(t.get("outcome", "")).casefold() == outcome.casefold()
    ]
    if len(matches) != 1:
        raise PriceHistoryError("Market has no unambiguous requested outcome token")
    token_id = str(matches[0].get("token_id") or matches[0].get("tokenId") or "")
    if not _TOKEN_ID.fullmatch(token_id):
        raise PriceHistoryError("Market returned an invalid outcome token ID")
    all_ids = [
        str(t.get("token_id") or t.get("tokenId") or "") for t in tokens if isinstance(t, dict)
    ]
    if len(all_ids) != len(set(all_ids)):
        raise PriceHistoryError("Market response contains duplicate outcome token IDs")
    return token_id


def _parse_history(payload: Any, start_ts: int, end_ts: int) -> list[tuple[int, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
        raise PriceHistoryError("CLOB price history response has no history array")
    rows: list[tuple[int, str]] = []
    for raw in payload["history"]:
        if not isinstance(raw, dict) or "t" not in raw or "p" not in raw:
            raise PriceHistoryError("Malformed history row: required t/p missing")
        try:
            ts_decimal = Decimal(str(raw["t"]))
            price = Decimal(str(raw["p"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PriceHistoryError("Malformed history timestamp or price") from exc
        if not ts_decimal.is_finite() or ts_decimal != ts_decimal.to_integral_value():
            raise PriceHistoryError("Non-integer history timestamp")
        ts = int(ts_decimal)
        if not price.is_finite() or not (Decimal("0") <= price <= Decimal("1")):
            raise PriceHistoryError("Price outside valid binary outcome range")
        if start_ts <= ts < end_ts:
            rows.append((ts, str(price)))
    return rows


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> Any:
    for attempt in range(3):
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise PriceHistoryError("Polymarket historical market data unavailable") from exc
        if response.status_code == 200:
            try:
                return response.json()
            except (ValueError, TypeError) as exc:
                raise PriceHistoryError("Polymarket returned invalid JSON") from exc
        if response.status_code in (429, 502, 503, 504) and attempt < 2:
            await asyncio.sleep(0.2 * (2**attempt))
            continue
        raise PriceHistoryError(f"Polymarket history HTTP {response.status_code}")
    raise PriceHistoryError("Polymarket history retries exhausted")


async def load_token_price_history(
    condition_id: str,
    start_date: str,
    end_date: str,
    *,
    outcome: str = "Yes",
) -> HistoricalPriceRows:
    """Resolve condition -> exact outcome token, then read bounded CLOB time series.

    The 'end_date' is inclusive on the user-facing date selector; the wire
    timestamp range is [start date UTC, midnight after end date UTC).
    Failure or partial coverage must never be disguised as a normal backtest.
    """
    if not isinstance(condition_id, str) or not _CONDITION_ID.fullmatch(condition_id):
        raise PriceHistoryError("Invalid Polymarket condition ID (expected 0x + 64 hex)")
    if outcome not in ("Yes", "No"):
        raise PriceHistoryError("Only explicit Yes/No outcome history is supported")
    start, end = _window(start_date, end_date)
    endpoint = POLYMARKET_CLOB_API.rstrip("/")
    all_points: dict[int, str] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        market = await _get_json(client, f"{endpoint}/markets/{condition_id}")
        token_id = _parse_market_tokens(market, condition_id, outcome)

        cursor = start
        while cursor < end:
            next_cursor = min(cursor + timedelta(days=_CHUNK_DAYS), end)
            left = int(cursor.timestamp())
            right = int(next_cursor.timestamp())
            payload = await _get_json(
                client,
                f"{endpoint}/prices-history",
                params={
                    "market": token_id,
                    "startTs": left - 1,
                    "endTs": right,
                    "fidelity": _FIDELITY_MINUTES,
                },
            )
            for ts, price in _parse_history(payload, left, right):
                previous = all_points.get(ts)
                if previous is not None and Decimal(previous) != Decimal(price):
                    raise PriceHistoryError("Contradictory history prices at the same timestamp")
                all_points[ts] = price
            cursor = next_cursor

    if not all_points:
        raise PriceHistoryError("No historical prices for this outcome token and date range")

    times = sorted(all_points)
    max_gap = max((b - a for a, b in zip(times, times[1:], strict=False)), default=0)
    # The CLOB sample series does not expose historical depth. It is always
    # indicative, and too-sparse series should not become completed backtests.
    incomplete = (
        len(times) < 30
        or times[0] - int(start.timestamp()) > 86400
        or int(end.timestamp()) - times[-1] > 86400
        or max_gap > 4 * 3600
    )
    rows = [
        {
            "t": ts,
            "timestamp": datetime.fromtimestamp(ts, tz=UTC).isoformat(),
            "price": float(Decimal(all_points[ts])),
        }
        for ts in times
    ]
    canonical = json.dumps(
        {
            "condition_id": condition_id.lower(),
            "token_id": token_id,
            "outcome": outcome,
            "prices": [(ts, all_points[ts]) for ts in times],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    provenance = {
        "source": "polymarket_clob_prices_history_v1",
        "source_url": f"{endpoint}/prices-history",
        "condition_id": condition_id.lower(),
        "token_id": token_id,
        "outcome": outcome,
        "start_utc": start.isoformat(),
        "end_exclusive_utc": end.isoformat(),
        "first_observed_at": rows[0]["timestamp"],
        "last_observed_at": rows[-1]["timestamp"],
        "fetched_at": datetime.now(UTC).isoformat(),
        "data_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "sample_count": len(rows),
        "max_gap_seconds": max_gap,
        "fidelity_minutes": _FIDELITY_MINUTES,
        "price_only": True,
        "quality": "insufficient_data" if incomplete else "indicative_price_only",
    }
    if incomplete:
        raise PriceHistoryError(
            "Insufficient historical price coverage for the selected outcome/date range"
        )
    return HistoricalPriceRows(rows, provenance)
