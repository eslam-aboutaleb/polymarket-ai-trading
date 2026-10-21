"""Validated, read-only Polymarket CLOB token price history for research backtests.

CLOB /prices-history accepts an outcome *token ID*, not a condition ID.
These observations are indicative price samples, not executable bid/ask quotes.
No missing price is ever fabricated and any upstream failure fails the run closed.

Ownership split: the shared venue client in ``polymarket_service`` owns the
transport (house timeout, bounded retries on transient upstream statuses); this
adapter owns the request contract, the row validation, the dataset provenance
and the cache of quality-valid observations.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.services.market_data.polymarket_service import (
    POLYMARKET_CLOB_API,
    get_polymarket_service,
    market_data_cache,
)

logger = logging.getLogger(__name__)
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_ID = re.compile(r"^[0-9]{1,80}$")
_CHUNK_DAYS = 30
_MAX_REQUESTS = 48
_FIDELITY_MINUTES = 60
_MIN_SAMPLES = 30
_MAX_GAP_SECONDS = 4 * 3600
_EDGE_TOLERANCE_SECONDS = 86400
_CACHE_TTL_SECONDS = 900

# Source identity. ``source``/``source_schema_version`` name the adapter that
# produced the rows; ``feed_version`` names the upstream sampling contract.
SOURCE_SCHEMA_VERSION = "polymarket_clob_prices_history_v1"
FEED_VERSION = "prices_history_feed_v1"
SOURCE_PREFERENCE = "polymarket_clob_prices_history"
RESOLUTION_RULE_VERSION = "clob_winner_resolution_v1"
QUALITY_INDICATIVE = "indicative_price_only"

# Failure reason -> the quality label a failed run carries. A failed run is
# never labelled "insufficient data" by default: the label names the cause.
FAILURE_QUALITY_BY_REASON = {
    "invalid_request": "invalid_request",
    "market_unavailable": "upstream_unavailable",
    "market_http_error": "upstream_unavailable",
    "market_invalid_payload": "invalid_source",
    "market_closed": "market_closed",
    "upstream_unavailable": "upstream_unavailable",
    "upstream_http_error": "upstream_unavailable",
    "upstream_invalid_payload": "invalid_source",
    "invalid_source": "invalid_source",
    "empty_history": "empty_history",
    "non_monotonic": "non_monotonic_series",
    "contradictory_prices": "invalid_source",
    "insufficient_coverage": "insufficient_coverage",
}


class PriceHistoryError(ValueError):
    """Expected data-quality/provider error: never silently becomes a zero-return run.

    ``reason`` names the cause (see ``FAILURE_QUALITY_BY_REASON``) and
    ``context`` carries whatever provenance was known when the failure hit, so
    a failed run can still show what was requested and what went wrong.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "price_history_error",
        context: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.context: dict[str, Any] = dict(context or {})


@dataclass(frozen=True)
class HistoricalPriceQuery:
    """One validated price-history request."""

    condition_id: str
    outcome_token_id: str
    start_utc: datetime
    end_utc: datetime
    resolution_rule_version: str
    fidelity_minutes: int
    source_preference: str


@dataclass(frozen=True)
class ObservedPrice:
    """One sampled CLOB price observation with its own provenance."""

    token_id: str
    condition_id: str
    outcome: str
    timestamp: datetime
    available_at: datetime
    price_decimal: Decimal
    source_url: str
    source_schema_version: str
    quality: str


@dataclass(frozen=True)
class MarketTokenMapping:
    """Outcome token mapping plus the market lifecycle state of the payload."""

    token_id: str
    outcome: str
    market_closed: bool
    market_active: bool
    token_winner: bool | None
    market_end_utc: datetime | None


class HistoricalPriceRows(list):
    """Price rows plus immutable dataset provenance for the backtest result."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        provenance: dict[str, Any],
        observations: tuple[ObservedPrice, ...] = (),
    ):
        super().__init__(rows)
        self.provenance = provenance
        self.observations = tuple(observations)


def _window(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    except (ValueError, TypeError) as exc:
        raise PriceHistoryError(
            "Invalid history date; use YYYY-MM-DD", reason="invalid_request"
        ) from exc
    if start >= end:
        raise PriceHistoryError(
            "History start date must precede end date", reason="invalid_request"
        )
    if end > datetime.now(UTC) + timedelta(days=1):
        raise PriceHistoryError(
            "History end date cannot be in the future", reason="invalid_request"
        )
    if math.ceil((end - start).total_seconds() / (_CHUNK_DAYS * 86400)) > _MAX_REQUESTS:
        raise PriceHistoryError(
            "Historical date range exceeds the safe request limit", reason="invalid_request"
        )
    return start, end


def price_window(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    """The validated UTC window for a user-facing date range."""
    return _window(start_date, end_date)


def _failure(message: str, reason: str, provenance: dict[str, Any]) -> PriceHistoryError:
    """Build a failure that keeps the provenance collected so far."""
    failed = {**provenance, "quality": FAILURE_QUALITY_BY_REASON.get(reason, "history_error")}
    return PriceHistoryError(message, reason=reason, context={"provenance": failed})


def _base_provenance(query: HistoricalPriceQuery, outcome: str) -> dict[str, Any]:
    endpoint = POLYMARKET_CLOB_API.rstrip("/")
    return {
        "source": SOURCE_SCHEMA_VERSION,
        "source_schema_version": SOURCE_SCHEMA_VERSION,
        "feed_version": FEED_VERSION,
        "source_url": f"{endpoint}/prices-history",
        "condition_id": query.condition_id,
        "token_id": query.outcome_token_id,
        "outcome": outcome,
        "start_utc": query.start_utc.isoformat(),
        "end_exclusive_utc": query.end_utc.isoformat(),
        "fidelity_minutes": query.fidelity_minutes,
        "resolution_rule_version": query.resolution_rule_version,
        "source_preference": query.source_preference,
        "price_only": True,
        "quality": "pending",
    }


def _parse_market_end(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("end_date_iso") or payload.get("end_date") or payload.get("endDate")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_market_tokens(
    payload: Any,
    condition_id: str,
    outcome: str,
    provenance: dict[str, Any] | None = None,
) -> MarketTokenMapping:
    provenance = provenance or {}
    if not isinstance(payload, dict):
        raise _failure(
            "CLOB market response must be a JSON object", "market_invalid_payload", provenance
        )
    identity = payload.get("condition_id") or payload.get("conditionId")
    if not isinstance(identity, str) or identity.lower() != condition_id.lower():
        raise _failure(
            "Market lookup returned a different or unknown condition ID",
            "market_invalid_payload",
            provenance,
        )
    tokens = payload.get("tokens")
    if not isinstance(tokens, list):
        raise _failure(
            "Market lookup has no outcome token mapping", "market_invalid_payload", provenance
        )
    matches = [
        t
        for t in tokens
        if isinstance(t, dict) and str(t.get("outcome", "")).casefold() == outcome.casefold()
    ]
    if len(matches) != 1:
        raise _failure(
            "Market has no unambiguous requested outcome token",
            "market_invalid_payload",
            provenance,
        )
    token_id = str(matches[0].get("token_id") or matches[0].get("tokenId") or "")
    if not _TOKEN_ID.fullmatch(token_id):
        raise _failure(
            "Market returned an invalid outcome token ID", "market_invalid_payload", provenance
        )
    all_ids = [
        str(t.get("token_id") or t.get("tokenId") or "") for t in tokens if isinstance(t, dict)
    ]
    if len(all_ids) != len(set(all_ids)):
        raise _failure(
            "Market response contains duplicate outcome token IDs",
            "market_invalid_payload",
            provenance,
        )
    winner = matches[0].get("winner")
    return MarketTokenMapping(
        token_id=token_id,
        outcome=outcome,
        market_closed=bool(payload.get("closed")),
        market_active=bool(payload.get("active", True)),
        token_winner=winner if isinstance(winner, bool) else None,
        market_end_utc=_parse_market_end(payload),
    )


def _parse_history(
    payload: Any,
    start_ts: int,
    end_ts: int,
    provenance: dict[str, Any] | None = None,
) -> list[tuple[int, str]]:
    provenance = provenance or {}
    if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
        raise _failure(
            "CLOB price history response has no history array",
            "upstream_invalid_payload",
            provenance,
        )
    rows: list[tuple[int, str]] = []
    for raw in payload["history"]:
        if not isinstance(raw, dict) or "t" not in raw or "p" not in raw:
            raise _failure(
                "Malformed history row: required t/p missing",
                "upstream_invalid_payload",
                provenance,
            )
        try:
            ts_decimal = Decimal(str(raw["t"]))
            price = Decimal(str(raw["p"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise _failure(
                "Malformed history timestamp or price", "upstream_invalid_payload", provenance
            ) from exc
        if not ts_decimal.is_finite() or ts_decimal != ts_decimal.to_integral_value():
            raise _failure("Non-integer history timestamp", "upstream_invalid_payload", provenance)
        ts = int(ts_decimal)
        if not price.is_finite() or not (Decimal("0") <= price <= Decimal("1")):
            raise _failure(
                "Price outside valid binary outcome range", "upstream_invalid_payload", provenance
            )
        if start_ts <= ts < end_ts:
            rows.append((ts, str(price)))
    return rows


async def _fetch_market(condition_id: str, provenance: dict[str, Any]) -> Any:
    """Resolve the condition through the shared Polymarket venue client."""
    try:
        return await get_polymarket_service().get_market_outcome_tokens(condition_id)
    except httpx.HTTPStatusError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        raise _failure(
            f"Polymarket market lookup HTTP {status}", "market_http_error", provenance
        ) from exc
    except httpx.HTTPError as exc:
        raise _failure(
            "Polymarket historical market data unavailable", "market_unavailable", provenance
        ) from exc
    except ValueError as exc:
        raise _failure(
            "Polymarket returned invalid JSON", "market_invalid_payload", provenance
        ) from exc


async def _fetch_price_history(
    token_id: str,
    left: int,
    right: int,
    fidelity_minutes: int,
    provenance: dict[str, Any],
) -> Any:
    """Read one bounded CLOB price-history chunk through the shared venue client."""
    try:
        return await get_polymarket_service().get_token_price_history(
            token_id, left - 1, right, fidelity_minutes
        )
    except httpx.HTTPStatusError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        raise _failure(
            f"Polymarket history HTTP {status}", "upstream_http_error", provenance
        ) from exc
    except httpx.HTTPError as exc:
        raise _failure(
            "Polymarket historical market data unavailable", "upstream_unavailable", provenance
        ) from exc
    except ValueError as exc:
        raise _failure(
            "Polymarket returned invalid JSON", "upstream_invalid_payload", provenance
        ) from exc


def _history_cache_key(query: HistoricalPriceQuery) -> str:
    return (
        f"price_history:{query.outcome_token_id}:"
        f"{int(query.start_utc.timestamp())}:{int(query.end_utc.timestamp())}:"
        f"{query.fidelity_minutes}"
    )


def _token_cache_key(query: HistoricalPriceQuery, outcome: str) -> str:
    return f"price_history_token:{query.condition_id}:{outcome}"


def _observation_to_dict(observation: ObservedPrice) -> dict[str, Any]:
    return {
        "token_id": observation.token_id,
        "condition_id": observation.condition_id,
        "outcome": observation.outcome,
        "timestamp": observation.timestamp.isoformat(),
        "available_at": observation.available_at.isoformat(),
        "price_decimal": str(observation.price_decimal),
        "source_url": observation.source_url,
        "source_schema_version": observation.source_schema_version,
        "quality": observation.quality,
    }


def _observation_from_dict(raw: dict[str, Any]) -> ObservedPrice:
    return ObservedPrice(
        token_id=str(raw["token_id"]),
        condition_id=str(raw["condition_id"]),
        outcome=str(raw["outcome"]),
        timestamp=datetime.fromisoformat(str(raw["timestamp"])),
        available_at=datetime.fromisoformat(str(raw["available_at"])),
        price_decimal=Decimal(str(raw["price_decimal"])),
        source_url=str(raw["source_url"]),
        source_schema_version=str(raw["source_schema_version"]),
        quality=str(raw["quality"]),
    )


def _dataset_checksum(provenance: dict[str, Any], observations: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"provenance": provenance, "observations": observations},
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rows_from_observations(observations: list[ObservedPrice]) -> list[dict[str, Any]]:
    return [
        {
            "t": int(observation.timestamp.timestamp()),
            "timestamp": observation.timestamp.isoformat(),
            "price": float(observation.price_decimal),
        }
        for observation in observations
    ]


def _read_cached_history(
    cache_key: str,
) -> tuple[dict[str, Any], list[ObservedPrice]] | None:
    """Return a cached dataset only when its checksum still matches."""
    entry = market_data_cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    provenance = entry.get("provenance")
    observations = entry.get("observations")
    checksum = entry.get("checksum")
    if not isinstance(provenance, dict) or not isinstance(observations, list):
        logger.warning("Discarding malformed price-history cache entry %s", cache_key)
        market_data_cache.delete(cache_key)
        return None
    if not isinstance(checksum, str) or _dataset_checksum(provenance, observations) != checksum:
        logger.warning("Corrupted price-history cache entry %s; recomputing", cache_key)
        market_data_cache.delete(cache_key)
        return None
    try:
        parsed = [_observation_from_dict(raw) for raw in observations]
    except (KeyError, TypeError, ValueError, ArithmeticError):
        logger.warning("Discarding unreadable price-history cache entry %s", cache_key)
        market_data_cache.delete(cache_key)
        return None
    return provenance, parsed


def _write_cached_history(
    cache_key: str,
    provenance: dict[str, Any],
    observations: list[ObservedPrice],
) -> None:
    payload = [_observation_to_dict(observation) for observation in observations]
    market_data_cache.set(
        cache_key,
        {
            "provenance": provenance,
            "observations": payload,
            "checksum": _dataset_checksum(provenance, payload),
        },
        ttl_seconds=_CACHE_TTL_SECONDS,
    )


def _mapping_from_cache(raw: Any) -> MarketTokenMapping | None:
    if not isinstance(raw, dict):
        return None
    token_id = raw.get("token_id")
    if not isinstance(token_id, str) or not _TOKEN_ID.fullmatch(token_id):
        return None
    end_raw = raw.get("market_end_utc")
    end_utc: datetime | None = None
    if isinstance(end_raw, str):
        try:
            end_utc = datetime.fromisoformat(end_raw)
        except ValueError:
            end_utc = None
    winner = raw.get("token_winner")
    return MarketTokenMapping(
        token_id=token_id,
        outcome=str(raw.get("outcome", "")),
        market_closed=bool(raw.get("market_closed")),
        market_active=bool(raw.get("market_active", True)),
        token_winner=winner if isinstance(winner, bool) else None,
        market_end_utc=end_utc,
    )


def _write_cached_token(
    query: HistoricalPriceQuery, outcome: str, mapping: MarketTokenMapping
) -> None:
    market_data_cache.set(
        _token_cache_key(query, outcome),
        {
            "token_id": mapping.token_id,
            "outcome": outcome,
            "market_closed": mapping.market_closed,
            "market_active": mapping.market_active,
            "token_winner": mapping.token_winner,
            "market_end_utc": (
                mapping.market_end_utc.isoformat() if mapping.market_end_utc else None
            ),
        },
        ttl_seconds=_CACHE_TTL_SECONDS,
    )


async def _resolve_outcome_token(
    query: HistoricalPriceQuery,
    outcome: str,
    provenance: dict[str, Any],
) -> MarketTokenMapping:
    """Resolve condition -> outcome token, reusing a cached mapping when present."""
    cached = _mapping_from_cache(market_data_cache.get(_token_cache_key(query, outcome)))
    if cached is not None:
        mapping = cached
    else:
        payload = await _fetch_market(query.condition_id, provenance)
        mapping = _parse_market_tokens(payload, query.condition_id, outcome, provenance)
    _reject_resolved_market(mapping, query, provenance)
    return mapping


def _reject_resolved_market(
    mapping: MarketTokenMapping,
    query: HistoricalPriceQuery,
    provenance: dict[str, Any],
) -> None:
    """Fail explicitly when the market settled before the requested window.

    A market that closed and resolved before the window has no price history
    in it. That is a different failure from "the feed is too sparse", and it
    must not be reported as generic insufficient coverage.
    """
    if not (mapping.market_closed or mapping.token_winner):
        return
    if mapping.market_end_utc is None or mapping.market_end_utc > query.start_utc:
        return
    raise _failure(
        "Market is already closed or resolved before the requested window",
        "market_closed",
        provenance,
    )


async def load_token_price_history(
    condition_id: str,
    start_date: str,
    end_date: str,
    *,
    outcome: str = "Yes",
    fidelity_minutes: int = _FIDELITY_MINUTES,
) -> HistoricalPriceRows:
    """Resolve condition -> exact outcome token, then read bounded CLOB time series.

    The 'end_date' is inclusive on the user-facing date selector; the wire
    timestamp range is [start date UTC, midnight after end date UTC).
    Failure or partial coverage must never be disguised as a normal backtest.
    """
    if not isinstance(condition_id, str) or not _CONDITION_ID.fullmatch(condition_id):
        raise PriceHistoryError(
            "Invalid Polymarket condition ID (expected 0x + 64 hex)", reason="invalid_request"
        )
    if outcome not in ("Yes", "No"):
        raise PriceHistoryError(
            "Only explicit Yes/No outcome history is supported", reason="invalid_request"
        )
    if isinstance(fidelity_minutes, bool) or not isinstance(fidelity_minutes, int):
        raise PriceHistoryError("Invalid price-history fidelity", reason="invalid_request")
    if fidelity_minutes <= 0:
        raise PriceHistoryError("Invalid price-history fidelity", reason="invalid_request")
    start, end = _window(start_date, end_date)

    query = HistoricalPriceQuery(
        condition_id=condition_id.lower(),
        outcome_token_id="",
        start_utc=start,
        end_utc=end,
        resolution_rule_version=RESOLUTION_RULE_VERSION,
        fidelity_minutes=fidelity_minutes,
        source_preference=SOURCE_PREFERENCE,
    )
    provenance = _base_provenance(query, outcome)

    mapping = await _resolve_outcome_token(query, outcome, provenance)
    query = replace(query, outcome_token_id=mapping.token_id)
    provenance["token_id"] = mapping.token_id

    cache_key = _history_cache_key(query)
    cached = _read_cached_history(cache_key)
    if cached is not None:
        cached_provenance, cached_observations = cached
        return HistoricalPriceRows(
            _rows_from_observations(cached_observations),
            cached_provenance,
            tuple(cached_observations),
        )

    all_points: dict[int, str] = {}
    last_seen: int | None = None
    fetched_at = datetime.now(UTC)
    provenance["fetched_at"] = fetched_at.isoformat()

    cursor = query.start_utc
    while cursor < query.end_utc:
        next_cursor = min(cursor + timedelta(days=_CHUNK_DAYS), query.end_utc)
        left = int(cursor.timestamp())
        right = int(next_cursor.timestamp())
        payload = await _fetch_price_history(
            query.outcome_token_id, left, right, query.fidelity_minutes, provenance
        )
        for ts, price in _parse_history(payload, left, right, provenance):
            previous = all_points.get(ts)
            if previous is not None and Decimal(previous) != Decimal(price):
                raise _failure(
                    "Contradictory history prices at the same timestamp",
                    "contradictory_prices",
                    provenance,
                )
            # An upstream series that goes backwards in time is a broken feed,
            # not something to silently re-sort: the run must fail closed.
            if last_seen is not None and ts < last_seen:
                raise _failure(
                    "Upstream history timestamps are not monotonically increasing",
                    "non_monotonic",
                    provenance,
                )
            last_seen = ts if last_seen is None else max(last_seen, ts)
            all_points[ts] = price
        cursor = next_cursor

    if not all_points:
        raise _failure(
            "No historical prices for this outcome token and date range",
            "empty_history",
            provenance,
        )

    times = sorted(all_points)
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    max_gap = max(gaps, default=0)
    gap_count = sum(1 for gap in gaps if gap > _MAX_GAP_SECONDS)
    observations = [
        ObservedPrice(
            token_id=query.outcome_token_id,
            condition_id=query.condition_id,
            outcome=outcome,
            timestamp=datetime.fromtimestamp(ts, tz=UTC),
            available_at=fetched_at,
            price_decimal=Decimal(all_points[ts]),
            source_url=provenance["source_url"],
            source_schema_version=SOURCE_SCHEMA_VERSION,
            quality=QUALITY_INDICATIVE,
        )
        for ts in times
    ]
    rows = _rows_from_observations(observations)
    canonical = json.dumps(
        {
            "condition_id": query.condition_id,
            "token_id": query.outcome_token_id,
            "outcome": outcome,
            "prices": [(ts, all_points[ts]) for ts in times],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    provenance.update(
        {
            "first_observed_at": rows[0]["timestamp"],
            "last_observed_at": rows[-1]["timestamp"],
            "data_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "sample_count": len(rows),
            "max_gap_seconds": max_gap,
            "gap_count": gap_count,
            "quality": QUALITY_INDICATIVE,
        }
    )

    # The CLOB sample series does not expose historical depth. It is always
    # indicative, and too-sparse series should not become completed backtests.
    incomplete = (
        len(times) < _MIN_SAMPLES
        or times[0] - int(query.start_utc.timestamp()) > _EDGE_TOLERANCE_SECONDS
        or int(query.end_utc.timestamp()) - times[-1] > _EDGE_TOLERANCE_SECONDS
        or max_gap > _MAX_GAP_SECONDS
    )
    if incomplete:
        raise _failure(
            "Insufficient historical price coverage for the selected outcome/date range",
            "insufficient_coverage",
            provenance,
        )

    # Only a quality-valid dataset is cached; an upstream failure never
    # becomes cached empty data.
    _write_cached_history(cache_key, provenance, observations)
    _write_cached_token(query, outcome, mapping)
    return HistoricalPriceRows(rows, provenance, tuple(observations))
