"""Shared fee-aware net-edge gate for taker strategies.

    net_edge = gross_edge - fee - half_spread - expected_slippage - margin

All terms are in probability units per share (one share pays 1 USDC when it
wins), so they are directly comparable to a probability gap. The fee comes from
``valuation_fees.archived_taker_fee``; unknown, unsupported or contradictory fee
metadata abstains and is never treated as zero. A reversal prices both legs.

This module decides whether a taker order may be attempted. It never claims a
trade is profitable: a positive net edge is only a model estimate.
"""

import json
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import httpx

from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API
from app.services.market_data.valuation_fees import archived_taker_fee

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
_FEE_CACHE_TTL_SECONDS = 300.0
_HTTP_TIMEOUT_SECONDS = 5.0

Side = Literal["BUY", "SELL"]


class Abstain(ValueError):
    """The gate cannot price this order, so it must not be placed."""


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal  # shares


@dataclass(frozen=True)
class BookSide:
    """Executable levels for one token: asks ascending, bids descending."""

    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()


@dataclass(frozen=True)
class Leg:
    """One taker order: BUY lifts asks, SELL hits bids."""

    side: Side
    price: Decimal  # reference price used for the gross edge
    shares: Decimal
    fee_metadata: dict[str, Any] | None
    book: BookSide | None
    fee_per_share: Decimal | None = None  # already-priced fee (e.g. from book evidence)


@dataclass(frozen=True)
class LegCost:
    side: str
    price: str
    fee: str
    half_spread: str
    slippage: str


@dataclass(frozen=True)
class NetEdgeDecision:
    allowed: bool
    reason: str
    gross_edge: Decimal
    fee: Decimal
    half_spread: Decimal
    slippage: Decimal
    margin: Decimal
    net_edge: Decimal | None
    min_net_edge: Decimal
    legs: tuple[LegCost, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "gross_edge": str(self.gross_edge),
            "fee": str(self.fee),
            "half_spread": str(self.half_spread),
            "slippage": str(self.slippage),
            "margin": str(self.margin),
            "net_edge": None if self.net_edge is None else str(self.net_edge),
            "min_net_edge": str(self.min_net_edge),
            "legs": [leg.__dict__ for leg in self.legs],
        }


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def taker_fee_per_share(price: Decimal, fee_metadata: dict[str, Any] | None) -> Decimal:
    """Taker fee for one share at ``price``; abstains on unknown metadata."""
    if not fee_metadata or "gamma" not in fee_metadata or "clob" not in fee_metadata:
        raise Abstain("ABSTAIN_FEE_METADATA_MISSING")
    if not Decimal(0) < price < Decimal(1):
        raise Abstain("ABSTAIN_INVALID_PRICE")
    try:
        fee, _version = archived_taker_fee(fee_metadata, [{"price": str(price), "shares": "1"}])
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise Abstain(
            str(exc) if str(exc).startswith("ABSTAIN") else "ABSTAIN_FEE_UNPRICEABLE"
        ) from exc
    return fee


def half_spread(book: BookSide | None) -> Decimal:
    if book is None or not book.bids or not book.asks:
        raise Abstain("ABSTAIN_SPREAD_UNKNOWN")
    spread = book.asks[0].price - book.bids[0].price
    if spread < 0:
        raise Abstain("ABSTAIN_CROSSED_BOOK")
    return spread / 2


def expected_slippage(side: Side, book: BookSide | None, shares: Decimal) -> Decimal:
    """VWAP distance from the touch for ``shares``; abstains on thin depth."""
    levels = None if book is None else (book.asks if side == "BUY" else book.bids)
    if not levels or shares <= 0:
        raise Abstain("ABSTAIN_DEPTH_UNKNOWN")
    remaining, cost = shares, Decimal(0)
    for level in levels:
        take = min(remaining, level.size)
        cost += take * level.price
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        raise Abstain("ABSTAIN_INSUFFICIENT_DEPTH")
    vwap = cost / shares
    return abs(vwap - levels[0].price)


def net_edge(
    gross_edge: Any,
    legs: Sequence[Leg],
    *,
    margin: Any,
    min_net_edge: Any = 0,
) -> NetEdgeDecision:
    """Gate one or more taker legs. Never raises; abstentions are decisions."""
    gross, margin_d, minimum = _dec(gross_edge), _dec(margin), _dec(min_net_edge)
    fee = spread = slip = Decimal(0)
    costs: list[LegCost] = []
    try:
        if not legs:
            raise Abstain("ABSTAIN_NO_LEGS")
        for leg in legs:
            if leg.fee_per_share is not None:
                leg_fee = leg.fee_per_share
            else:
                leg_fee = taker_fee_per_share(leg.price, leg.fee_metadata)
            leg_spread = half_spread(leg.book)
            leg_slip = expected_slippage(leg.side, leg.book, leg.shares)
            fee, spread, slip = fee + leg_fee, spread + leg_spread, slip + leg_slip
            costs.append(
                LegCost(leg.side, str(leg.price), str(leg_fee), str(leg_spread), str(leg_slip))
            )
    except Abstain as exc:
        return NetEdgeDecision(
            False, str(exc), gross, fee, spread, slip, margin_d, None, minimum, tuple(costs)
        )
    net = gross - fee - spread - slip - margin_d
    allowed = net > 0 and net >= minimum
    reason = "OK" if allowed else "NET_EDGE_NOT_POSITIVE" if net <= 0 else "NET_EDGE_BELOW_MINIMUM"
    return NetEdgeDecision(
        allowed, reason, gross, fee, spread, slip, margin_d, net, minimum, tuple(costs)
    )


def log_decision(strategy: str, decision: NetEdgeDecision, **context: Any) -> None:
    """One audit line per decision (paper and live), with every cost term."""
    logger.info(
        "taker_net_edge_decision %s",
        json.dumps({"strategy": strategy, **context, **decision.to_dict()}, sort_keys=True),
    )


# ── Live inputs (network) ───────────────────────────────────────────────

_fee_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _normalise_fee_metadata(clob: dict[str, Any], gamma: dict[str, Any]) -> dict[str, Any]:
    """Live Gamma rows carry no ``version`` key; archived captures label it ``v1``.

    The label is added only for the exact schedule shape the archived function
    supports (exponent 1, taker only). Any other shape is left untouched so the
    function abstains.
    """
    gamma = dict(gamma)
    schedule = gamma.get("feeSchedule") or {}
    if (
        "version" not in gamma
        and schedule.get("exponent") == 1
        and schedule.get("takerOnly") is True
    ):
        gamma["version"] = "v1"
    return {"clob": dict(clob), "gamma": gamma}


async def fetch_fee_metadata(
    condition_id: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """Gamma + CLOB fee metadata for a market, cached; ``None`` when unavailable."""
    now = time.monotonic()
    cached = _fee_cache.get(condition_id)
    if cached and now - cached[0] < _FEE_CACHE_TTL_SECONDS:
        return cached[1]
    owned = client is None
    client = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
    metadata = None
    try:
        gamma_resp = await client.get(
            f"{GAMMA_API}/markets", params={"condition_ids": condition_id}
        )
        clob_resp = await client.get(f"{POLYMARKET_CLOB_API}/markets/{condition_id}")
        if gamma_resp.status_code == 200 and clob_resp.status_code == 200:
            rows = gamma_resp.json()
            clob = clob_resp.json()
            if isinstance(rows, list) and rows and isinstance(clob, dict):
                metadata = _normalise_fee_metadata(clob, rows[0])
    except (httpx.HTTPError, ValueError):
        metadata = None
    finally:
        if owned:
            await client.aclose()
    _fee_cache[condition_id] = (now, metadata)
    return metadata


def parse_book(payload: dict[str, Any]) -> BookSide:
    """CLOB ``/book`` payload (object levels) into sorted executable levels."""

    def levels(rows: Iterable[Any], reverse: bool) -> tuple[Level, ...]:
        parsed = []
        for row in rows or []:
            try:
                parsed.append(Level(_dec(row["price"]), _dec(row["size"])))
            except (KeyError, TypeError, ArithmeticError):
                raise Abstain("ABSTAIN_MALFORMED_BOOK") from None
        return tuple(sorted(parsed, key=lambda lv: lv.price, reverse=reverse))

    return BookSide(bids=levels(payload.get("bids"), True), asks=levels(payload.get("asks"), False))


async def fetch_book(token_id: str, client: httpx.AsyncClient | None = None) -> BookSide | None:
    owned = client is None
    client = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS)
    try:
        response = await client.get(f"{POLYMARKET_CLOB_API}/book", params={"token_id": token_id})
        if response.status_code != 200:
            return None
        return parse_book(response.json())
    except (httpx.HTTPError, ValueError, Abstain):
        return None
    finally:
        if owned:
            await client.aclose()
