"""Manifold venue adapter (plan 05 Phase B).

Manifold Markets is a **play-money** venue: its unit of account is Mana
(and Sweepcoins on the sweepstakes side), not cash, so a Manifold price
is a *sentiment* reading rather than a quote anyone can trade against.
That is why this adapter hard-codes ``is_real_money=False`` and why the
mismatch detector never treats a Manifold price as the executable leg of
a cross-venue signal.

The venue publishes a documented, unauthenticated JSON market list
(``GET /v0/markets``) that needs no key and no certificate. Only binary
markets map onto the shared shape: Manifold's multiple-choice, numeric
and poll questions are multi-outcome and have no single yes price, so
they are skipped rather than approximated. ``fetch_markets`` makes that
one GET per refresh and degrades to ``[]`` on any HTTP or timeout
error; ``parse_markets`` (and the pure ``parse_manifold_markets`` it
delegates to) normalizes the payload with no I/O at all.

Read-only and signal-only. Off unless ``venue_manifold_enabled`` is set,
TTL-cached so a page load never reaches Manifold directly, and never
places, cancels or sizes an order.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from app.schemas.market import NormalizedMarket, canonical_raw_hash
from app.utils.cache import get_cache

logger = logging.getLogger(__name__)

MANIFOLD_API_BASE = os.environ.get(
    "MANIFOLD_API_BASE",
    "https://api.manifold.markets/v0",
)
MANIFOLD_WEB_BASE = "https://manifold.markets"

COMMON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "PolymarketBot/1.0",
}

FETCH_TIMEOUT_SECONDS = 15.0
CACHE_TTL_SECONDS = 120

# Only binary markets carry one probability; every other outcome type is
# multi-outcome and has no single yes price to compare.
BINARY_OUTCOME_TYPES = {"BINARY"}

CACHE = get_cache("venue:manifold")


def _probability(value: Any) -> float | None:
    """Parse a Manifold probability field into [0, 1], else None."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _number(value: Any) -> float:
    """Parse a Manifold count/amount field; 0.0 when unusable."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0.0 else 0.0


def _close_datetime(value: Any) -> datetime | None:
    """Manifold stamps close/creation times in epoch **milliseconds**."""
    if value is None:
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0.0:
        return None
    # Tolerate second- and microsecond-precision payloads too.
    if stamp > 1e14:
        stamp /= 1e6
    elif stamp > 1e11:
        stamp /= 1e3
    return datetime.fromtimestamp(stamp, tz=UTC)


def _question_url(item: dict[str, Any]) -> str:
    url = str(item.get("url") or "").strip()
    if url:
        return url
    slug = str(item.get("slug") or "").strip()
    return f"{MANIFOLD_WEB_BASE}/{slug}" if slug else MANIFOLD_WEB_BASE


def parse_manifold_markets(raw: list[dict]) -> list[NormalizedMarket]:
    """Pure Manifold payload → normalized rows. No I/O, no clock reads.

    A row is dropped when it is not a binary open market: no id (it
    cannot be linked back), a non-binary outcome type (no single price),
    an already resolved market (the price is final, not a quote), or no
    probability at all (nothing to compare).
    """
    markets: list[NormalizedMarket] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        market_id = str(item.get("id") or "").strip()
        if not market_id:
            continue
        outcome_type = str(item.get("outcomeType") or "").strip().upper()
        if outcome_type not in BINARY_OUTCOME_TYPES:
            continue
        if bool(item.get("isResolved")):
            continue
        yes_price = _probability(item.get("probability"))
        if yes_price is None:
            continue
        markets.append(
            NormalizedMarket(
                venue="manifold",
                market_id=market_id,
                question=str(item.get("question") or "").strip(),
                yes_price=yes_price,
                no_price=1.0 - yes_price,
                volume_24h=_number(item.get("volume24Hours")),
                liquidity=_number(item.get("totalLiquidity")),
                end_date=_close_datetime(item.get("closeTime")),
                url=_question_url(item),
                # Play money: sentiment only, never the executable leg.
                is_real_money=False,
                raw_hash=canonical_raw_hash(item),
            )
        )
    return markets


class ManifoldAdapter:
    """Adapter for Manifold's public JSON market list (play money)."""

    name = "manifold"

    async def _get_markets(self, limit: int) -> list[dict] | None:
        """One GET of the market list; None when the call is unusable."""
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS,
                headers=COMMON_HEADERS,
            ) as client:
                response = await client.get(
                    f"{MANIFOLD_API_BASE}/markets",
                    params={"limit": limit},
                )
            if response.status_code != 200:
                logger.warning("Manifold /markets returned %s", response.status_code)
                return None
            payload = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Manifold /markets request failed: %s", exc)
            return None
        return payload if isinstance(payload, list) else None

    async def fetch_markets(self, *, limit: int = 100) -> list[dict]:
        """Markets from Manifold, TTL-cached; ``[]`` on any failure."""
        cached = CACHE.get(f"markets:{limit}")
        if cached is not None:
            return cached
        markets = await self._get_markets(limit)
        if markets is None:
            return []
        # Only successful responses are cached: a cached empty list
        # would suppress the next good fetch for a whole TTL.
        CACHE.set(f"markets:{limit}", markets, CACHE_TTL_SECONDS)
        return markets

    def parse_markets(self, raw: list[dict]) -> list[NormalizedMarket]:
        return parse_manifold_markets(raw)
