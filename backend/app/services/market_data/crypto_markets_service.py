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
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings
from app.utils.scheduler_lock import scheduler_heartbeat

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA_API = get_settings().polymarket_gamma_api.rstrip("/")

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


def _json_list(value: Any) -> list[Any] | None:
    """Gamma returns list-valued market fields as JSON-encoded strings."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _extract_tokens(market: dict[str, Any]) -> dict[str, str]:
    """Map outcome labels to token ids for the up/down legs."""
    tokens: dict[str, str] = {}
    clob_ids = _json_list(market.get("clobTokenIds"))
    outcomes = _json_list(market.get("outcomes"))
    if clob_ids and outcomes and len(clob_ids) == len(outcomes):
        for outcome, token_id in zip(outcomes, clob_ids, strict=True):
            label = str(outcome).strip().lower()
            if label in _UP_OUTCOMES and token_id:
                tokens["up"] = str(token_id)
            elif label in _DOWN_OUTCOMES and token_id:
                tokens["down"] = str(token_id)
        if tokens:
            return tokens
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
    outcome_prices = _json_list(market.get("outcomePrices"))
    if outcome_prices and len(outcome_prices) >= 2:
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


_SLUG_WINDOW = re.compile(r"-updown-(\d+)(m|h)-", re.IGNORECASE)


def _window_from_event(event: dict[str, Any], market: dict[str, Any]) -> int | None:
    """Window length when the title carries a time range instead of "5m".

    Current Gamma titles read "Bitcoin Up or Down - November 25, 6:55PM-7:00PM
    ET", so the length comes from the event slug (``btc-updown-5m-<epoch>``) or,
    failing that, from the start/end times.
    """
    slug = str(event.get("slug") or market.get("slug") or "")
    found = _SLUG_WINDOW.search(slug)
    if found:
        minutes = int(found.group(1)) * (60 if found.group(2).lower() == "h" else 1)
        return minutes if minutes in WINDOW_MINUTES else None
    start = _parse_iso_datetime(market.get("eventStartTime") or event.get("startTime"))
    end = _parse_iso_datetime(market.get("endDate") or event.get("endDate"))
    if start is None or end is None:
        return None
    minutes = round((end - start).total_seconds() / 60)
    return minutes if minutes in WINDOW_MINUTES else None


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
            ) or _window_from_event(event, market)
            if window_minutes is None:
                continue
            condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
            if not condition_id:
                continue
            token_ids = _extract_tokens(market)
            if not token_ids:
                continue
            # ``eventStartTime`` is the window boundary; ``startDate`` is when
            # the market was created (often the day before) and only a fallback.
            start_date = _parse_iso_datetime(
                market.get("eventStartTime")
                or event.get("startTime")
                or market.get("startDate")
                or event.get("startDate")
            )
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


def window_slugs(now: datetime | None = None) -> list[str]:
    """Deterministic Gamma slugs for the current and next 5m/15m windows."""
    moment = now or datetime.now(UTC)
    slugs: list[str] = []
    for window_minutes in (5, 15):
        start = current_window_start(window_minutes, moment)
        for offset in (0, 1):
            for symbol in SYMBOLS:
                epoch = start + offset * window_minutes * 60
                slugs.append(f"{symbol.lower()}-updown-{window_minutes}m-{epoch}")
    return slugs


async def _fetch_slug_events(slugs: list[str]) -> list[dict[str, Any]]:
    """Fetch one Gamma event per slug (windows are named by their start epoch)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:

        async def one(slug: str) -> list[dict[str, Any]]:
            try:
                response = await client.get(f"{POLYMARKET_GAMMA_API}/events", params={"slug": slug})
                body = response.json() if response.status_code == 200 else []
            except Exception as exc:
                logger.debug("Gamma slug %s failed: %s", slug, exc)
                return []
            return body if isinstance(body, list) else []

        batches = await asyncio.gather(*(one(slug) for slug in slugs))
    return [event for batch in batches for event in batch]


async def refresh_markets() -> int:
    """Rebuild the market mapping from the Gamma API.

    Returns the number of mapped markets; failures leave the previous
    mapping intact and return the stale count.
    """
    global _markets, _last_refresh_at
    try:
        events = await _fetch_gamma_events()
        # The generic listing is capped (and misses windows that just opened),
        # so the current and next 5m/15m windows are fetched by slug as well.
        events = events + await _fetch_slug_events(window_slugs())
    except Exception as exc:
        logger.warning("Crypto market discovery failed: %s", exc)
        return len(_markets)
    parsed = parse_crypto_markets(events)
    _markets = parsed
    _last_refresh_at = datetime.now(UTC).timestamp()
    _sync_live_book_tokens(parsed)
    logger.debug("Crypto markets refreshed: %d active windows", len(parsed))
    return len(parsed)


def _sync_live_book_tokens(markets: dict[tuple[str, int, int], CryptoMarket]) -> None:
    """Tell the live order-book feed which outcome tokens to follow."""
    try:
        if not get_settings().latency_arb_book_feed_enabled:
            return
        from app.services.market_data.live_book_service import get_live_book_feed

        get_live_book_feed().set_tokens(
            {token for m in markets.values() for token in m.token_ids.values()}
        )
    except Exception:
        logger.warning("Live book token sync failed", exc_info=True)


def with_live_prices(market: CryptoMarket) -> CryptoMarket | None:
    """A copy of ``market`` priced from the live order book, or ``None``.

    Prices are the mid of the best bid/ask of each outcome token. ``None``
    means the live book is missing or not fresh; the caller must not trade on
    the 30 s Gamma price instead.
    """
    from app.services.market_data.live_book_service import get_live_book_feed

    feed = get_live_book_feed()
    up = feed.quote(market.up_token_id)
    down = feed.quote(market.down_token_id)
    if up is None or down is None:
        return None
    return CryptoMarket(
        symbol=market.symbol,
        window_minutes=market.window_minutes,
        window_start_epoch=market.window_start_epoch,
        condition_id=market.condition_id,
        token_ids=dict(market.token_ids),
        prices={"up": up.mid, "down": down.mid},
        question=market.question,
        start_date=market.start_date,
        end_date=market.end_date,
    )


def live_quote_for_condition(condition_id: str) -> dict[str, Any] | None:
    """Live up/down book quotes for a tracked crypto window, or ``None``.

    Read-only view for the AI context. It reads the same live order-book feed
    the edge calculation uses, so the numbers an agent sees match the numbers
    the engine trades on. Returns ``None`` when the condition is not a tracked
    window or the live book is not fresh.
    """
    from app.services.market_data.live_book_service import get_live_book_feed

    wanted = condition_id.lower()
    for market in _markets.values():
        if market.condition_id.lower() != wanted:
            continue
        feed = get_live_book_feed()
        up, down = feed.quote(market.up_token_id), feed.quote(market.down_token_id)
        if up is None or down is None:
            return None
        return {
            "symbol": market.symbol,
            "window_minutes": market.window_minutes,
            "window_start_epoch": market.window_start_epoch,
            "condition_id": market.condition_id,
            "up": up.to_dict(),
            "down": down.to_dict(),
            "source": "clob_ws",
        }
    return None


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
