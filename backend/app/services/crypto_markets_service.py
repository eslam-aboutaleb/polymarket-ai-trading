"""Polymarket crypto up/down market discovery for the latency engine (plan 06).

Finds the BTC/ETH/SOL 5m/15m/1h "up or down" markets on Polymarket
via the Gamma API and maintains a mapping

    (symbol, window_minutes, window_start_epoch) -> CryptoMarket

where ``window_start_epoch`` is the UTC window boundary (seconds).
The mapping refreshes every 30 seconds.

Wrong-window mapping is the engine's main failure mode (trading the
wrong market), so every lookup is validated against the current UTC
window before trading: ``window_start_matches`` returns ``True`` only
when the market's window start equals the boundary the wall clock is
currently inside. The engine additionally refuses to trade a market
whose window start does not match (see ``latency_arb_service``).

The Gamma ``/events`` endpoint does not reliably honour tag/search
query params, so this service over-fetches (ordered by start date,
newest first — the crypto windows are the freshest events) and filters
server-side by title pattern, mirroring ``polymarket_service``.
"""

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.utils.scheduler_lock import scheduler_heartbeat

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA_API = os.environ.get("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com")

# Symbols and window sizes the engine trades.
SYMBOLS = ("BTC", "ETH", "SOL")
WINDOW_MINUTES = (5, 15, 60)

# Market-discovery refresh interval (seconds).
REFRESH_INTERVAL_SECONDS = float(os.environ.get("LATENCY_ARB_MARKET_REFRESH", "30"))

# Over-fetch bound for the Gamma /events call.
FETCH_LIMIT = 500

HTTP_TIMEOUT_SECONDS = 15.0

# Title patterns: a market belongs to a symbol/window when its title
# names the asset and the window length.
_SYMBOL_PATTERNS: dict[str, re.Pattern[str]] = {
    "BTC": re.compile(r"\b(bitcoin|btc)\b", re.IGNORECASE),
    "ETH": re.compile(r"\b(ethereum|eth)\b", re.IGNORECASE),
    "SOL": re.compile(r"\b(solana|sol)\b", re.IGNORECASE),
}
_WINDOW_PATTERNS: dict[int, re.Pattern[str]] = {
    5: re.compile(r"\b(5m|5\s*[- ]?min|5\s*minutes?|five\s*minutes?)\b", re.IGNORECASE),
    15: re.compile(r"\b(15m|15\s*[- ]?min|15\s*minutes?|fifteen\s*minutes?)\b", re.IGNORECASE),
    60: re.compile(
        r"\b(1h|1\s*[- ]?hr|1\s*[- ]?hour|1\s*hour|60\s*minutes?|hourly)\b",
        re.IGNORECASE,
    ),
}
# Titles must describe a direction market (up/down).
_DIRECTION_PATTERN = re.compile(r"up\s*or\s*down|up/down|direction", re.IGNORECASE)

# Outcome labels mapped to the up/down legs.
_UP_OUTCOMES = {"up", "yes"}
_DOWN_OUTCOMES = {"down", "no"}


@dataclass
class CryptoMarket:
    """One active crypto up/down market."""

    symbol: str
    window_minutes: int
    window_start_epoch: int
    condition_id: str
    token_ids: dict[str, str] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    question: str = ""
    start_date: datetime | None = None
    end_date: datetime | None = None

    @property
    def up_token_id(self) -> str:
        return self.token_ids.get("up", "")

    @property
    def down_token_id(self) -> str:
        return self.token_ids.get("down", "")

    @property
    def up_price(self) -> float:
        return self.prices.get("up", 0.5)

    @property
    def down_price(self) -> float:
        return self.prices.get("down", 0.5)

    def to_dict(self) -> dict[str, Any]:
        """Serializable snapshot for the opportunities board."""
        return {
            "symbol": self.symbol,
            "window_minutes": self.window_minutes,
            "window_start_epoch": self.window_start_epoch,
            "condition_id": self.condition_id,
            "token_ids": dict(self.token_ids),
            "prices": dict(self.prices),
            "question": self.question,
        }


def current_window_start(window_minutes: int, now: datetime | None = None) -> int:
    """Epoch seconds of the current UTC window boundary.

    A 5m window starting at 02:25 covers 02:25:00–02:29:59; the
    boundary is the largest multiple of ``window_minutes`` minutes
    not after ``now``.
    """
    moment = now or datetime.now(UTC)
    epoch = int(moment.timestamp())
    return epoch - (epoch % (window_minutes * 60))


def window_start_matches(
    market: CryptoMarket,
    window_minutes: int,
    now: datetime | None = None,
) -> bool:
    """Validate that a market's window start is the current UTC window."""
    return market.window_start_epoch == current_window_start(window_minutes, now)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _market_title(event: dict[str, Any], market: dict[str, Any]) -> str:
    for key in ("title", "question"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("question", "groupItemTitle", "title"):
        value = market.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_tokens(market: dict[str, Any]) -> dict[str, str]:
    """Map outcome labels to token ids for the up/down legs."""
    tokens: dict[str, str] = {}
    for token in market.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        token_id = str(token.get("token_id") or token.get("tokenId") or "")
        if not token_id:
            continue
        outcome = str(token.get("outcome") or "").strip().lower()
        if outcome in _UP_OUTCOMES:
            tokens["up"] = token_id
        elif outcome in _DOWN_OUTCOMES:
            tokens["down"] = token_id
    return tokens


def _extract_prices(market: dict[str, Any]) -> dict[str, float]:
    """Current up/down prices from the market payload."""
    prices: dict[str, float] = {}
    outcome_prices = market.get("outcomePrices")
    if isinstance(outcome_prices, (list, tuple)) and len(outcome_prices) >= 2:
        try:
            prices["up"] = float(outcome_prices[0])
            prices["down"] = float(outcome_prices[1])
            return prices
        except (IndexError, TypeError, ValueError):
            pass
    for token in market.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome") or "").strip().lower()
        try:
            price = float(token.get("price"))
        except (TypeError, ValueError):
            continue
        if outcome in _UP_OUTCOMES:
            prices["up"] = price
        elif outcome in _DOWN_OUTCOMES:
            prices["down"] = price
    return prices


def _align_window_start(epoch_seconds: int, window_minutes: int) -> int:
    """Snap a market start to its window boundary (tolerates clock noise)."""
    return int(epoch_seconds) - (int(epoch_seconds) % (window_minutes * 60))


def parse_crypto_markets(
    events: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[tuple[str, int, int], CryptoMarket]:
    """Filter raw Gamma events into the (symbol, window, start) mapping.

    Pure function (no I/O) so discovery is unit-testable with
    synthetic payloads. Only markets whose aligned window start is
    the current, previous or next window are retained — older
    windows are untradeable and would bloat the map.
    """
    moment = now or datetime.now(UTC)
    markets: dict[tuple[str, int, int], CryptoMarket] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_markets = event.get("markets")
        if not isinstance(event_markets, (list, tuple)):
            continue
        for market in event_markets:
            if not isinstance(market, dict):
                continue
            title = _market_title(event, market)
            if not title or not _DIRECTION_PATTERN.search(title):
                continue
            symbol = next(
                (
                    candidate
                    for candidate, pattern in _SYMBOL_PATTERNS.items()
                    if pattern.search(title)
                ),
                None,
            )
            if symbol is None:
                continue
            window_minutes = next(
                (minutes for minutes, pattern in _WINDOW_PATTERNS.items() if pattern.search(title)),
                None,
            )
            if window_minutes is None:
                continue
            condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
            if not condition_id:
                continue
            token_ids = _extract_tokens(market)
            if not token_ids:
                continue
            start_date = _parse_iso_datetime(market.get("startDate") or event.get("startDate"))
            if start_date is None:
                continue
            window_start = _align_window_start(int(start_date.timestamp()), window_minutes)
            # Retain the current and adjacent windows only.
            current_start = current_window_start(window_minutes, moment)
            if not (
                current_start - window_minutes * 60
                <= window_start
                <= current_start + 2 * window_minutes * 60
            ):
                continue
            end_date = _parse_iso_datetime(market.get("endDate") or event.get("endDate"))
            markets[(symbol, window_minutes, window_start)] = CryptoMarket(
                symbol=symbol,
                window_minutes=window_minutes,
                window_start_epoch=window_start,
                condition_id=condition_id,
                token_ids=token_ids,
                prices=_extract_prices(market),
                question=title,
                start_date=start_date,
                end_date=end_date,
            )
    return markets


# ── Runtime state ────────────────────────────────────────────────

_markets: dict[tuple[str, int, int], CryptoMarket] = {}
_last_refresh_at: float = 0.0

_refresh_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_running = False


async def _fetch_gamma_events(limit: int = FETCH_LIMIT) -> list[dict[str, Any]]:
    """Fetch active Gamma events (newest start date first)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{POLYMARKET_GAMMA_API}/events",
            params={
                "limit": limit,
                "active": True,
                "closed": False,
                "order": "startDate",
                "ascending": False,
            },
        )
    if response.status_code != 200:
        logger.warning("Gamma /events returned %s", response.status_code)
        return []
    events = response.json()
    return events if isinstance(events, list) else []


async def refresh_markets() -> int:
    """Rebuild the market mapping from the Gamma API.

    Returns the number of mapped markets; failures leave the previous
    mapping intact and return the stale count.
    """
    global _markets, _last_refresh_at
    try:
        events = await _fetch_gamma_events()
    except Exception as exc:
        logger.warning("Crypto market discovery failed: %s", exc)
        return len(_markets)
    parsed = parse_crypto_markets(events)
    _markets = parsed
    _last_refresh_at = datetime.now(UTC).timestamp()
    logger.debug("Crypto markets refreshed: %d active windows", len(parsed))
    return len(parsed)


def get_market(
    symbol: str,
    window_minutes: int,
    window_start_epoch: int | None = None,
) -> CryptoMarket | None:
    """Look up a market by (symbol, window, window start).

    ``window_start_epoch`` defaults to the current UTC window
    boundary. Returns ``None`` when no market is mapped.
    """
    start = (
        current_window_start(window_minutes)
        if window_start_epoch is None
        else int(window_start_epoch)
    )
    return _markets.get((symbol.upper(), int(window_minutes), start))


def get_active_markets(now: datetime | None = None) -> list[CryptoMarket]:
    """All mapped markets for the current UTC windows."""
    moment = now or datetime.now(UTC)
    active: list[CryptoMarket] = []
    for window_minutes in WINDOW_MINUTES:
        start = current_window_start(window_minutes, moment)
        for symbol in SYMBOLS:
            market = _markets.get((symbol, window_minutes, start))
            if market is not None:
                active.append(market)
    return active


def markets_stale(max_age_seconds: float = REFRESH_INTERVAL_SECONDS) -> bool:
    """True when the mapping is older than the refresh interval."""
    if not _last_refresh_at:
        return True
    age = datetime.now(UTC).timestamp() - _last_refresh_at
    return age > max_age_seconds


async def _periodic_refresh(stop_event: asyncio.Event) -> None:
    """Refresh the market mapping on a timer."""
    while not stop_event.is_set():
        await refresh_markets()
        scheduler_heartbeat("latency_arb")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=REFRESH_INTERVAL_SECONDS)


# ── Lifecycle ────────────────────────────────────────────────────


async def start_crypto_markets_refresh() -> None:
    """Start the market-discovery refresh loop (scheduler-locked).

    The "latency_arb" scheduler lock is acquired by
    ``latency_arb_service.start_latency_arb_engine``; this helper
    only manages the refresh task.
    """
    global _refresh_task, _stop_event, _running
    if _running:
        return
    _stop_event = asyncio.Event()
    _running = True
    _refresh_task = asyncio.create_task(_periodic_refresh(_stop_event))
    logger.info("Crypto market discovery refresh started")


async def stop_crypto_markets_refresh() -> None:
    """Stop the market-discovery refresh task."""
    global _refresh_task, _stop_event, _running
    if not _running:
        return
    _running = False
    if _stop_event is not None:
        _stop_event.set()
    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Crypto markets refresh stop error: %s", exc)
    _refresh_task = None
    _stop_event = None
    logger.info("Crypto market discovery refresh stopped")
