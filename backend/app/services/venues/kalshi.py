"""Kalshi venue adapter (plan 05 Phase A).

Kalshi is the first adapter because it is a CFTC-regulated US exchange
with a public REST API that needs no API key and no certificates — the
same ``trade-api/v2/markets`` payload a browser fetches — and because
its prices are real money rather than play money.

The endpoint returns one dict per open market with dollar-string prices
(``yes_bid_dollars``/``yes_ask_dollars``/...), 24-hour volume and open
interest. ``fetch_markets`` makes that single GET per refresh and
degrades to ``[]`` on any HTTP or timeout error; ``parse_markets``
(and the pure ``parse_kalshi_markets`` it delegates to) turns the
payload into ``NormalizedMarket`` rows without touching the network.

Read-only and signal-only: nothing here places or cancels an order. The
adapter is off unless ``venue_kalshi_enabled`` is set, and its response
is TTL-cached so page loads never reach Kalshi directly.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx

from app.schemas.market import NormalizedMarket, canonical_raw_hash
from app.utils.cache import get_cache
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Public, unauthenticated market-data endpoint (verified live at
# implementation time against the same documented host the Kalshi
# quick-start uses).
KALSHI_API_BASE = os.environ.get(
    "KALSHI_API_BASE",
    "https://api.elections.kalshi.com/trade-api/v2",
)
KALSHI_WEB_BASE = "https://kalshi.com"

# Kalshi quotes every price as a dollar string ("0.0700"); a binary
# outcome settles at $1, so anything outside [0, 1] is not a price.
COMMON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "PolymarketBot/1.0",
}

FETCH_TIMEOUT_SECONDS = 15.0
CACHE_TTL_SECONDS = 120

# Kalshi publishes no liquidity figure; open interest (contracts
# outstanding) is the closest public proxy and is stored as such.
CACHE = get_cache("venue:kalshi")


def _dollars(value: Any) -> float | None:
    """Parse a Kalshi dollar-string price field into a probability, else None."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _number(value: Any) -> float:
    """Parse a Kalshi numeric field (volume, open interest); 0.0 when unusable."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0.0 else 0.0


def _mid(bid: Any, ask: Any) -> float | None:
    """Midpoint of both sides of a book; None when either side is absent."""
    low = _dollars(bid)
    high = _dollars(ask)
    if low is None or high is None or low <= 0.0 or high <= 0.0:
        return None
    return (low + high) / 2.0


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=utc_now().tzinfo)
    return parsed


def _yes_price(item: dict[str, Any]) -> float | None:
    """Mid of the yes book, falling back to the last traded price.

    A zero bid means there is no bid at all (an illiquid tail), so the
    midpoint is only used when both sides are quoted; otherwise the
    last trade is the best real price the market has printed.
    """
    mid = _mid(item.get("yes_bid_dollars"), item.get("yes_ask_dollars"))
    if mid is not None:
        return mid
    last = _dollars(item.get("last_price_dollars"))
    return last if last and last > 0.0 else None


def _no_price(item: dict[str, Any], yes_price: float | None) -> float | None:
    """Mid of the no book, derived from yes when one side is unquoted."""
    mid = _mid(item.get("no_bid_dollars"), item.get("no_ask_dollars"))
    if mid is not None:
        return mid
    if yes_price is not None:
        return 1.0 - yes_price
    return None


def _liquidity(item: dict[str, Any]) -> float:
    """Open interest as the liquidity proxy (Kalshi publishes no depth)."""
    for key in ("liquidity", "liquidity_fp", "open_interest_fp"):
        value = _number(item.get(key))
        if value:
            return value
    return 0.0


def parse_kalshi_markets(raw: list[dict]) -> list[NormalizedMarket]:
    """Pure Kalshi payload → normalized rows. No I/O, no clock reads.

    Rows without a ticker or with a non-open status are skipped: they
    cannot be linked back to a venue page and cannot trade.
    """
    markets: list[NormalizedMarket] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip()
        if not ticker:
            continue
        status = str(item.get("status") or "active").strip().lower()
        if status not in {"active", "open"}:
            continue
        yes_price = _yes_price(item)
        markets.append(
            NormalizedMarket(
                venue="kalshi",
                market_id=ticker,
                question=str(item.get("title") or "").strip(),
                yes_price=yes_price,
                no_price=_no_price(item, yes_price),
                volume_24h=_number(item.get("volume_24h_fp")),
                liquidity=_liquidity(item),
                end_date=_parse_iso_datetime(item.get("close_time") or item.get("expiration_time")),
                url=f"{KALSHI_WEB_BASE}/markets/{ticker}",
                is_real_money=True,
                raw_hash=canonical_raw_hash(item),
            )
        )
    return markets


class KalshiAdapter:
    """Adapter for Kalshi's public REST market data."""

    name = "kalshi"

    async def _get_markets(self, limit: int) -> list[dict] | None:
        """One GET of the open markets; None when the call is unusable."""
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS,
                headers=COMMON_HEADERS,
            ) as client:
                response = await client.get(
                    f"{KALSHI_API_BASE}/markets",
                    params={"limit": limit, "status": "open"},
                )
            if response.status_code != 200:
                logger.warning("Kalshi /markets returned %s", response.status_code)
                return None
            payload = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Kalshi /markets request failed: %s", exc)
            return None
        markets = payload.get("markets") if isinstance(payload, dict) else None
        return markets if isinstance(markets, list) else None

    async def fetch_markets(self, *, limit: int = 100) -> list[dict]:
        """Open markets from Kalshi, TTL-cached; ``[]`` on any failure."""
        cached = CACHE.get(f"markets:{limit}")
        if cached is not None:
            return cached
        markets = await self._get_markets(limit)
        if markets is None:
            return []
        # Only successful responses are cached: a cached empty list would
        # suppress the next good fetch for a whole TTL.
        CACHE.set(f"markets:{limit}", markets, CACHE_TTL_SECONDS)
        return markets

    def parse_markets(self, raw: list[dict]) -> list[NormalizedMarket]:
        return parse_kalshi_markets(raw)
