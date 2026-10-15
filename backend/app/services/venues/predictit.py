"""PredictIt venue adapter (plan 05 Phase B).

PredictIt is a real-money US prediction market, so its prices are
comparable with a regulated exchange's — but it is also the venue with
the sharpest legal edges, which is why this adapter ships **off by
default** and why this docstring records them:

* **Jurisdiction.** PredictIt's terms restrict who may trade and on
  which markets, and non-US persons cannot trade at all. Reading public
  market data is not trading, but the operator of this deployment is
  responsible for determining whether their use of the venue (and of
  this data) is permitted where they are.
* **Access.** The public ``/api/marketdata/all/`` endpoint sits behind
  bot protection and answers a plain HTTP client with ``403``; this
  adapter never tries to defeat that. A ``403`` is handled as a normal
  venue outage: logged once, degraded to ``[]``, nothing cached.
* **Commercial use.** PredictIt's terms limit commercial use of its
  data. Everything this adapter reads is therefore treated as an
  internal **signal** for a human to review, never as a feed for a
  paid product or an order.
* **Read-only.** One GET per refresh, TTL-cached, no order placement,
  no credentials, no cookies.

The payload is a market list where each market holds a list of
contracts; a *contract* is the binary question, so one row per open
contract is emitted and the contract id is the ``market_id``. Prices
come from the published best bid/ask; when only one side is quoted the
top of book is used, and the last trade is the final fallback.
PredictIt publishes neither 24-hour volume nor depth on this endpoint,
so both are stored as ``0.0`` (unknown) rather than guessed.
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

PREDICTIT_API_BASE = os.environ.get(
    "PREDICTIT_API_BASE",
    "https://www.predictit.org",
)
PREDICTIT_WEB_BASE = PREDICTIT_API_BASE

COMMON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "PolymarketBot/1.0",
}

FETCH_TIMEOUT_SECONDS = 15.0
CACHE_TTL_SECONDS = 120

# Market and contract statuses that can still be priced.
OPEN_STATUSES = {"open", "trading"}

CACHE = get_cache("venue:predictit")


def _price(value: Any) -> float | None:
    """Parse a PredictIt share price into [0, 1], else None."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0.0 < parsed < 1.0 else None


def _mid(low: Any, high: Any) -> float | None:
    """Midpoint of the no side and the yes side of a contract's book."""
    sell = _price(low)
    buy = _price(high)
    if sell is None or buy is None or sell > buy:
        return None
    return (sell + buy) / 2.0


def _parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _market_name(market: dict[str, Any]) -> str:
    return str(market.get("name") or market.get("shortName") or "").strip()


def _contract_name(contract: dict[str, Any]) -> str:
    return str(contract.get("name") or contract.get("shortName") or "").strip()


def _question(market: dict[str, Any], contract: dict[str, Any], siblings: int) -> str:
    """Verbatim question for a contract.

    A one-contract market is the binary question itself; a multi-contract
    market needs the contract name, otherwise every contract of the
    market would normalize to the same key and collapse into one fake
    "mismatch" against itself.
    """
    name = _market_name(market)
    label = _contract_name(contract)
    if siblings < 2:
        return name
    return f"{name}: {label}" if label else name


def _yes_price(contract: dict[str, Any]) -> float | None:
    """Yes price: book mid, then the cheaper buy, then the last trade."""
    mid = _mid(contract.get("bestSellYesCost"), contract.get("bestBuyYesCost"))
    if mid is not None:
        return mid
    buy = _price(contract.get("bestBuyYesCost"))
    if buy is not None:
        return buy
    return _price(contract.get("lastTradePrice"))


def _no_price(contract: dict[str, Any], yes_price: float | None) -> float | None:
    mid = _mid(contract.get("bestSellNoCost"), contract.get("bestBuyNoCost"))
    if mid is not None:
        return mid
    if yes_price is not None:
        return 1.0 - yes_price
    return None


def parse_predictit_markets(raw: list[dict]) -> list[NormalizedMarket]:
    """Pure PredictIt payload → normalized rows. No I/O, no clock reads.

    Rows are skipped when the contract cannot be priced: a closed
    market or contract, no contract id (nothing to link back), or no
    price at all.
    """
    markets: list[NormalizedMarket] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        market_id = str(item.get("id") or "").strip()
        status = str(item.get("status") or "Open").strip().lower()
        if status not in OPEN_STATUSES or not market_id:
            continue
        contracts = item.get("contracts")
        if not isinstance(contracts, list):
            continue
        open_contracts = [
            contract
            for contract in contracts
            if isinstance(contract, dict)
            and str(contract.get("status") or "Open").strip().lower() in OPEN_STATUSES
        ]
        for contract in open_contracts:
            contract_id = str(contract.get("id") or "").strip()
            if not contract_id:
                continue
            yes_price = _yes_price(contract)
            if yes_price is None:
                continue
            question = _question(item, contract, len(open_contracts))
            if not question:
                continue
            end_date = _parse_datetime(contract.get("dateEnd") or item.get("dateEnd"))
            markets.append(
                NormalizedMarket(
                    venue="predictit",
                    market_id=contract_id,
                    question=question,
                    yes_price=yes_price,
                    no_price=_no_price(contract, yes_price),
                    volume_24h=0.0,
                    liquidity=0.0,
                    end_date=end_date,
                    url=f"{PREDICTIT_WEB_BASE}/markets/detail/{market_id}",
                    is_real_money=True,
                    raw_hash=canonical_raw_hash(contract),
                )
            )
    return markets


class PredictItAdapter:
    """Adapter for PredictIt's public market-data endpoint (real money)."""

    name = "predictit"

    async def _get_markets(self) -> list[dict] | None:
        """One GET of the market bundle; None when the call is unusable."""
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS,
                headers=COMMON_HEADERS,
            ) as client:
                response = await client.get(f"{PREDICTIT_API_BASE}/api/marketdata/all/")
            if response.status_code != 200:
                # 403 is the common case: bot protection, not an outage.
                # It is never worked around — the venue simply stays dark.
                logger.warning(
                    "PredictIt /api/marketdata/all returned %s (adapter stays off)",
                    response.status_code,
                )
                return None
            payload = response.json()
        except httpx.HTTPError as exc:
            logger.warning("PredictIt market data request failed: %s", exc)
            return None
        if isinstance(payload, dict):
            markets = payload.get("markets")
            return markets if isinstance(markets, list) else None
        return payload if isinstance(payload, list) else None

    async def fetch_markets(self, *, limit: int = 100) -> list[dict]:
        """Markets from PredictIt, TTL-cached; ``[]`` on any failure.

        ``limit`` caps the number of markets kept. PredictIt's endpoint
        is all-or-nothing — one bundle, no paging — so the whole payload
        is fetched once per TTL and the cap is applied here rather than
        upstream.
        """
        cached = CACHE.get("markets")
        if cached is not None:
            return cached[:limit]
        markets = await self._get_markets()
        if markets is None:
            return []
        # Only successful responses are cached, so one blocked minute
        # cannot suppress the next good fetch for a whole TTL.
        CACHE.set("markets", markets, CACHE_TTL_SECONDS)
        return markets[:limit]

    def parse_markets(self, raw: list[dict]) -> list[NormalizedMarket]:
        return parse_predictit_markets(raw)
