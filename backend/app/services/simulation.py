"""Paper-trading fill simulation (plan 08).

Paper results are ESTIMATES, not executions. Simulated fills are
produced by a depth-aware slippage model bounded by
``SLIPPAGE_SIM_MIN`` (default 0.3%) and ``SLIPPAGE_SIM_MAX``
(default 3%), scaled by ``size / book_depth`` and by
``size / volume_24hr``. A depth-aware model can still be optimistic
in fast or thin markets, so paper PnL must be read as an estimate
with configurable bounds — never as a forecast of live results.

The model is deterministic by default, which keeps paper runs
reproducible and makes slippage monotonically non-decreasing in
order size. Callers may pass an ``rng`` (``random.Random``) to
sample a bounded jitter term on top of the deterministic base.

Public API:
    simulate_fill(market, side, size, rng=None) -> dict
    simulate_market_maker_fill(market, side, size, order_price,
                               midpoint=None, rng=None) -> dict
    get_paper_summary(db, user_id) -> dict
    average_entry_price(db, user_id, token_id) -> float | None
"""

from __future__ import annotations

import contextlib
import random
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings

# Fallback bounds (fractions of price) used when settings are
# unavailable, e.g. before the app config is initialised.
DEFAULT_SLIPPAGE_SIM_MIN = 0.003  # 0.3%
DEFAULT_SLIPPAGE_SIM_MAX = 0.03  # 3%

# Default paper account balance (USDC).
DEFAULT_PAPER_BALANCE = 1000.0

# Weights of the depth / volume terms in the marketable-fill model.
_DEPTH_WEIGHT = 0.6
_VOLUME_WEIGHT = 0.4

# Weights of the band-distance / depth / volume terms in the
# market-maker fill model (band distance dominates: a band far from
# mid implies the market moved further against the fill).
_MAKER_BAND_WEIGHT = 0.5
_MAKER_DEPTH_WEIGHT = 0.3
_MAKER_VOLUME_WEIGHT = 0.2


def _slippage_bounds() -> tuple[float, float]:
    """Return the configured (min, max) simulation slippage fractions."""
    low = DEFAULT_SLIPPAGE_SIM_MIN
    high = DEFAULT_SLIPPAGE_SIM_MAX
    with contextlib.suppress(Exception):
        settings = get_settings()
        low = float(settings.slippage_sim_min)
        high = float(settings.slippage_sim_max)
    if low < 0.0:
        low = 0.0
    if high < low:
        high = low
    return low, high


def _field(market: Any, key: str, default: Any = None) -> Any:
    """Read a key from a dict-like or object-like market payload."""
    if market is None:
        return default
    if isinstance(market, dict):
        return market.get(key, default)
    return getattr(market, key, default)


def _best_bid_ask(market: Any) -> tuple[float, float]:
    """Extract the best bid/ask, falling back to a midpoint/price."""
    best_bid = _field(market, "best_bid")
    best_ask = _field(market, "best_ask")
    if best_bid is None or best_ask is None:
        mid = _field(market, "midpoint")
        if mid is None:
            mid = _field(market, "mid")
        if mid is None:
            mid = _field(market, "price")
        if mid is not None:
            if best_bid is None:
                best_bid = mid
            if best_ask is None:
                best_ask = mid
    if best_bid is None or best_ask is None:
        raise ValueError("market must provide best_bid/best_ask or a midpoint/price")
    return float(best_bid), float(best_ask)


def _depth_ratio(size: float, book_depth: Any) -> float:
    """Fraction of the book an order would walk, capped at 1.0.

    Unknown depth maps to a neutral 0.5 so paper fills on markets
    without book data sit mid-range rather than at the pessimistic cap.
    """
    try:
        depth = float(book_depth)
    except (TypeError, ValueError):
        return 0.5
    if depth <= 0:
        return 0.5
    return min(float(size) / depth, 1.0)


def _volume_ratio(size: float, volume_24hr: Any) -> float:
    """Order size relative to 24h volume, capped at 1.0 (neutral 0.5)."""
    try:
        volume = float(volume_24hr)
    except (TypeError, ValueError):
        return 0.5
    if volume <= 0:
        return 0.5
    return min(float(size) / volume, 1.0)


def _clamp_price(price: float) -> float:
    """Clamp a simulated price to the Polymarket [0.0001, 0.9999] range."""
    return min(max(price, 0.0001), 0.9999)


def _apply_jitter(
    fraction: float,
    low: float,
    high: float,
    rng: random.Random | None,
) -> float:
    """Sample a bounded ±10% jitter term when an RNG is supplied."""
    if rng is None:
        return fraction
    return min(max(fraction * (1.0 + rng.uniform(-0.1, 0.1)), low), high)


def simulate_fill(
    market: Any,
    side: str,
    size: float,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Simulate a marketable fill of ``size`` USDC on ``side``.

    The base price is the current best ask (BUY) or best bid (SELL).
    Slippage is sampled from the depth-aware model — bounded by
    ``SLIPPAGE_SIM_MIN``..``SLIPPAGE_SIM_MAX`` and scaled by
    ``size / book_depth`` and ``size / volume_24hr`` — and applied
    away from the base price.

    ``market`` is a dict-like (or object) with ``best_bid`` /
    ``best_ask`` (or a ``midpoint``/``price`` fallback) and optional
    ``book_depth`` and ``volume_24hr``.

    Returns ``{"fill_price", "slippage_bps"}``.
    """
    low, high = _slippage_bounds()
    best_bid, best_ask = _best_bid_ask(market)
    is_buy = str(side).upper() == "BUY"
    base = best_ask if is_buy else best_bid

    fraction = _DEPTH_WEIGHT * _depth_ratio(size, _field(market, "book_depth")) + (
        _VOLUME_WEIGHT
        * _volume_ratio(
            size,
            _field(market, "volume_24hr", _field(market, "volume")),
        )
    )
    fraction = low + (high - low) * min(max(fraction, 0.0), 1.0)
    fraction = _apply_jitter(fraction, low, high, rng)

    fill = base * (1.0 + fraction) if is_buy else base * (1.0 - fraction)
    return {
        "fill_price": round(_clamp_price(fill), 4),
        "slippage_bps": round(fraction * 10_000.0, 2),
    }


def simulate_market_maker_fill(
    market: Any,
    side: str,
    size: float,
    order_price: float,
    midpoint: float | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Simulate a market-maker band fill resting at ``order_price``.

    Unlike a marketable fill, a maker fill happens at the band price;
    the simulated cost is the adverse selection the band implies — the
    further the band sits from the midpoint, the further the market
    had to move to hit it. The slippage term therefore scales with the
    band distance from mid (plus the usual depth/volume terms) and is
    applied away from the band price.

    Returns ``{"fill_price", "slippage_bps", "band_distance"}``.
    """
    low, high = _slippage_bounds()
    best_bid, best_ask = _best_bid_ask(market)
    if midpoint is None:
        midpoint = (best_bid + best_ask) / 2.0
    midpoint = float(midpoint)
    band_distance = abs(float(order_price) - midpoint) / midpoint if midpoint > 0 else 0.0

    fraction = (
        _MAKER_BAND_WEIGHT * min(band_distance, 1.0)
        + _MAKER_DEPTH_WEIGHT * _depth_ratio(size, _field(market, "book_depth"))
        + _MAKER_VOLUME_WEIGHT
        * _volume_ratio(
            size,
            _field(market, "volume_24hr", _field(market, "volume")),
        )
    )
    fraction = low + (high - low) * min(max(fraction, 0.0), 1.0)
    fraction = _apply_jitter(fraction, low, high, rng)

    is_buy = str(side).upper() == "BUY"
    fill = float(order_price) * (1.0 + fraction)
    if not is_buy:
        fill = float(order_price) * (1.0 - fraction)
    return {
        "fill_price": round(_clamp_price(fill), 4),
        "slippage_bps": round(fraction * 10_000.0, 2),
        "band_distance": round(band_distance, 6),
    }


def get_paper_summary(db: Session, user_id: int) -> dict[str, Any]:
    """Paper-trading summary for a user, tracked separately from real equity.

    Paper PnL is the sum of ``pnl`` over the user's simulated trades;
    ``paper_balance`` is the configurable paper starting balance. Real
    equity is never touched by paper mode.
    """
    from app.models.user_settings import UserSettings
    from app.models.user_trade import UserTrade

    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    paper_balance = (
        float(settings.paper_balance)
        if settings is not None and settings.paper_balance is not None
        else DEFAULT_PAPER_BALANCE
    )
    simulated = (
        db.query(UserTrade)
        .filter(UserTrade.user_id == user_id, UserTrade.status == "simulated")
        .all()
    )
    paper_pnl = sum(float(t.pnl or 0.0) for t in simulated)
    return {
        "simulation_mode": bool(settings is not None and settings.simulation_mode),
        "paper_balance": round(paper_balance, 2),
        "paper_pnl": round(paper_pnl, 2),
        "simulated_trades": len(simulated),
    }


def average_entry_price(db: Session, user_id: int, token_id: str) -> float | None:
    """Volume-weighted average entry price for a user's token position.

    Considers executed and simulated buys; returns ``None`` when the
    user has no recorded entry (paper PnL is then left unset).
    """
    from app.models.user_trade import UserTrade

    rows = (
        db.query(UserTrade)
        .filter(
            UserTrade.user_id == user_id,
            UserTrade.token_id == token_id,
            UserTrade.action == "buy",
            UserTrade.status.in_(("executed", "simulated")),
        )
        .all()
    )
    notional = 0.0
    shares = 0.0
    for row in rows:
        price = float(row.price or 0.0)
        amount = float(row.amount or 0.0)
        if price <= 0.0 or amount <= 0.0:
            continue
        notional += amount
        shares += amount / price
    if shares <= 0.0:
        return None
    return notional / shares
