"""Fresh CLOB evidence for bounded copy orders."""

from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import httpx

from app.services.market_data.replay_capture import PublicVenueCapture
from app.utils.time import utc_now

MAX_QUOTE_AGE = timedelta(seconds=5)
SUPPORTED_TICKS = {Decimal(x) for x in (".1", ".01", ".001", ".0001")}


def copy_limit(side, source_price, slippage, tick):
    if side not in {"BUY", "SELL"} or tick not in SUPPORTED_TICKS:
        raise ValueError("Unsupported copy side or tick")
    source, drift = Decimal(str(source_price)), Decimal(str(slippage))
    if not (source.is_finite() and drift.is_finite() and 0 < source < 1 and 0 <= drift < 1):
        raise ValueError("Invalid copy price or slippage")
    buys = side == "BUY"
    raw = source * (1 + drift if buys else 1 - drift)
    limit = (raw / tick).to_integral_value(rounding=ROUND_FLOOR if buys else ROUND_CEILING) * tick
    if buys:
        limit = min(limit, 1 - tick)
    if not 0 < limit < 1:
        raise ValueError("Copy slippage bound has no legal venue price")
    return limit


def quote_plan(book, raw, side, source_price, slippage, notional, moment):
    expires = min(book.observed_at, book.source_at) + MAX_QUOTE_AGE
    if expires <= moment or book.source_at > moment or book.observed_at > moment:
        raise ValueError("Copy order book is stale or future-dated")
    limit = copy_limit(side, source_price, slippage, Decimal(str(raw["tick_size"])))
    size_price = limit if side == "BUY" else Decimal(str(source_price))
    shares = (Decimal(str(notional)) / size_price).quantize(Decimal(".01"), rounding=ROUND_FLOOR)
    minimum = Decimal(str(raw["min_order_size"]))
    if (
        book.bids
        and book.asks
        and max(x.price for x in book.bids) >= min(x.price for x in book.asks)
    ):
        raise ValueError("Copy book is crossed")
    levels = book.asks if side == "BUY" else book.bids
    available = sum(
        x.shares for x in levels if (x.price <= limit if side == "BUY" else x.price >= limit)
    )
    if not minimum.is_finite() or minimum <= 0 or shares < minimum or available < shares:
        raise ValueError("Insufficient executable copy depth within slippage bound")
    return limit, shares, expires


async def fresh_copy_quote(condition, token, side, price, slippage, notional):
    async with httpx.AsyncClient(timeout=4) as client:
        raw, book = await PublicVenueCapture(client).book(token, condition)
    limit, shares, expires = quote_plan(book, raw, side, price, slippage, notional, utc_now())
    return (
        limit,
        shares,
        expires,
        {
            "condition": condition,
            "token": token,
            "source_at": book.source_at.isoformat(),
            "observed_at": book.observed_at.isoformat(),
            "snapshot_id": book.snapshot_id,
            "tick_size": str(raw["tick_size"]),
            "neg_risk": raw["neg_risk"],
        },
    )
