"""Latency-arbitrage engine for Polymarket crypto up/down markets (plan 06).

Detects and acts on BTC/ETH/SOL 5m/15m/1h Up/Down mispricings by
reacting to Binance price moves before Polymarket odds adjust.
Ships paper-first; live mode is behind an explicit flag
(``LATENCY_ARB_LIVE``) and additionally requires the trading user's
``simulation_mode`` to be off.

Edge model
----------
For each active window the engine computes

    distance   = (current_price − window_open) / window_open
    t_remaining = fraction of the window left
    σ_realized  = realized per-window volatility from recent 1m klines
    σ_implied   = |Φ⁻¹(p_market)| / √t_remaining  (the market's own
                  z-score over the remaining window; a coin-flip market
                  carries no volatility information, so implied σ → 0)
    σ           = max(σ_realized, 2 × σ_implied)   (conservative)
    P(Up)       = Φ(distance / (σ · √t_remaining))  (Bachelier / log-normal)
    edge        = |P_model − p_market|

A candidate requires ``edge ≥ 3%`` (configurable), executable ask-side
share depth and a spread ≤ 2% (hard abort above, mirroring the
sell-side slippage guard).

Paper fills use captured native evidence and the shared paper journal. They are
modeled, never exchange-confirmed. Live BUY plans use the shared binary-book
adapter, known zero fees, executable ask depth and an expiring FOK limit. Missing
or invalid evidence abstains. Both live latency flags default off.

Risk gates (all must pass before an order):

* plan 01's shared ``apply_global_safety_caps`` (halt / monthly loss /
  drawdown / total-loss / position-cap / daily-loss layers),
* a per-strategy daily loss limit (configurable per user),
* a consecutive-loss circuit breaker (pause after 3 losses, resume
  after 10 minutes),
* a halt in the final 10s of a window unless ``LATENCY_ARB_LATE_ENTRY``,
* a latency budget: the feed→order path aborts when it exceeds
  ``LATENCY_ARB_MAX_LATENCY_MS`` (default 1500ms) — the edge only
  exists when Polymarket's lag dominates Binance's, so a slow path
  must never trade,
* max notional per trade ``LATENCY_ARB_MAX_NOTIONAL`` (default $50).

The engine measures and logs the Binance-feed→Polymarket latency
distribution every cycle (feed lag and total feed→order p50/p95).

Lifecycle follows plan 07's pattern: ``start_latency_arb_engine`` /
``stop_latency_arb_engine`` are scheduler-locked ("latency_arb") and
own the Binance feed, the market-discovery refresh and the engine loop.
"""

import asyncio
import contextlib
import json
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.repositories.trading.bots import TradingBotRepository
from app.repositories.trading.orders import TradingOrderRepository
from app.services.market_data import binance_ws_service, crypto_markets_service
from app.services.market_data.crypto_markets_service import CryptoMarket
from app.services.trading.execution.async_io import run_blocking_execution
from app.utils.cache import get_cache
from app.utils.database import SessionLocal
from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)

logger = logging.getLogger(__name__)

# Cross-plan imports are defensive so this module still loads while
# another plan's file is mid-edit. The pre-trade gate fails CLOSED
# (trades are rejected) when unavailable; the paper simulator rejects
# paper trades when unavailable.
try:
    from app.services.trading.pre_trade_gate import (
        apply_global_safety_caps,
        build_clob_client,
    )

    _PRE_TRADE_GATE_AVAILABLE = True
except ImportError:
    apply_global_safety_caps = None  # type: ignore[assignment]
    build_clob_client = None  # type: ignore[assignment]
    _PRE_TRADE_GATE_AVAILABLE = False

try:
    from app.services.analytics.simulation import simulate_fill

    _SIMULATION_AVAILABLE = True
except ImportError:
    simulate_fill = None  # type: ignore[assignment]
    _SIMULATION_AVAILABLE = False

try:
    from app.services.trading.copy_trade_service import _get_poly_proxy_wallet_address
except ImportError:
    _get_poly_proxy_wallet_address = None  # type: ignore[assignment]

# Fallback per-window volatility (0.5% of price) when kline history
# is too short to estimate realized σ.
DEFAULT_SIGMA = 0.005

# Probability bounds for the market price (Φ⁻¹ domain).
P_MIN = 0.0001
P_MAX = 0.9999

# Latency samples retained for the distribution log.
LATENCY_SAMPLES_MAX = 500

# Opportunities are served from the shared cache (other workers)
# with a short TTL; the in-memory list is the fallback.
OPPORTUNITIES_TTL_SECONDS = 60

# Strategy source recorded on UserTrade rows (plan 03 analytics).
STRATEGY_SOURCE = "latency_arb"


# ────────────── Normal distribution helpers ──────────────


def normal_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erf``."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's algorithm).

    Accurate to ~1.15e-9 relative error; sufficient for the
    implied-σ z-scores the edge model uses.
    """
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(
        (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    )


# ────────────── Edge model (pure functions) ──────────────


def compute_distance(current_price: float, window_open: float) -> float:
    """Fractional price move since the window opened."""
    if window_open <= 0.0:
        return 0.0
    return (float(current_price) - float(window_open)) / float(window_open)


def compute_t_remaining(
    window_start_epoch: int | float,
    window_minutes: int,
    now: datetime,
) -> float:
    """Fraction of the window remaining, clamped to [0, 1]."""
    window_end = float(window_start_epoch) + int(window_minutes) * 60.0
    total = int(window_minutes) * 60.0
    if total <= 0.0:
        return 0.0
    remaining = (window_end - now.timestamp()) / total
    return min(max(remaining, 0.0), 1.0)


def realized_volatility(closes: list[float], window_minutes: int) -> float:
    """Per-window realized σ from recent 1m kline closes.

    Computes the standard deviation of consecutive log returns
    (per-kline σ) and scales by √window_minutes to a per-window
    σ, matching the units of ``distance``.
    """
    if len(closes) < 2:
        return DEFAULT_SIGMA
    returns: list[float] = []
    for previous, current in zip(closes[:-1], closes[1:], strict=False):
        if previous > 0.0 and current > 0.0:
            returns.append(math.log(current / previous))
    if len(returns) < 2:
        return DEFAULT_SIGMA
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    per_kline_sigma = math.sqrt(variance)
    return per_kline_sigma * math.sqrt(int(window_minutes))


def implied_volatility(
    p_market: float,
    t_remaining: float,
) -> float:
    """Per-window σ implied by the market price.

    The market's z-score over the remaining window: the σ under
    which the market's current price is a 1σ-distance move. A
    coin-flip market (z = 0) carries no volatility information,
    so implied σ is 0 and the model falls back to realized σ.
    """
    if t_remaining <= 0.0:
        return 0.0
    clamped = min(max(p_market, P_MIN), P_MAX)
    z_score = normal_ppf(clamped)
    if not math.isfinite(z_score):
        return 0.0
    return abs(z_score) / math.sqrt(t_remaining)


def conservative_sigma(realized: float, implied: float) -> float:
    """Model-risk guard: max(realized, 2 × implied).

    Bachelier underestimates tails, so σ is never allowed below
    the realized level and is inflated to twice the market-implied
    level before any edge is trusted.
    """
    return max(float(realized), 2.0 * float(implied))


def probability_up(
    distance: float,
    sigma: float,
    t_remaining: float,
) -> float:
    """Bachelier / log-normal P(Up) = Φ(distance / (σ·√t)).

    As t → 0 an in-the-money window (distance > 0) converges to
    P = 1 and an out-of-the-money window to P = 0.
    """
    if t_remaining <= 0.0 or sigma <= 0.0:
        if distance > 0.0:
            return 1.0
        if distance < 0.0:
            return 0.0
        return 0.5
    return normal_cdf(distance / (float(sigma) * math.sqrt(t_remaining)))


def compute_edge(p_model: float, p_market: float) -> float:
    """Absolute probability gap between model and market."""
    return abs(float(p_model) - float(p_market))


@dataclass
class Opportunity:
    """One candidate mispricing for a symbol/window."""

    symbol: str
    window_minutes: int
    window_start_epoch: int
    side: str  # "up" or "down" — the underpriced leg
    p_model: float
    p_market: float
    edge: float
    distance: float
    t_remaining: float
    sigma: float
    current_price: float
    window_open: float
    market: CryptoMarket
    detected_at: datetime
    feed_lag_ms: float | None = None

    @property
    def window_end_epoch(self) -> int:
        return self.window_start_epoch + self.window_minutes * 60

    def to_dict(self) -> dict[str, Any]:
        """Serializable snapshot for the opportunities board."""
        return {
            "symbol": self.symbol,
            "window_minutes": self.window_minutes,
            "window_start_epoch": self.window_start_epoch,
            "window_end_epoch": self.window_end_epoch,
            "side": self.side,
            "p_model": round(self.p_model, 4),
            "p_market": round(self.p_market, 4),
            "edge": round(self.edge, 4),
            "distance": round(self.distance, 6),
            "t_remaining": round(self.t_remaining, 4),
            "sigma": round(self.sigma, 6),
            "current_price": self.current_price,
            "window_open": self.window_open,
            "condition_id": self.market.condition_id,
            "question": self.market.question,
            "token_ids": dict(self.market.token_ids),
            "prices": dict(self.market.prices),
            "feed_lag_ms": (round(self.feed_lag_ms, 1) if self.feed_lag_ms is not None else None),
            "detected_at": self.detected_at.isoformat(),
        }


def evaluate_opportunity(
    symbol: str,
    window_minutes: int,
    market: CryptoMarket,
    current_price: float,
    window_open: float,
    now: datetime,
    kline_closes: list[float],
    feed_lag_ms: float | None = None,
    edge_threshold: float | None = None,
) -> Opportunity | None:
    """Evaluate one symbol/window against the edge model.

    Returns an ``Opportunity`` when the model/market probability
    gap meets ``edge_threshold`` (default: the global setting),
    otherwise ``None``.
    """
    threshold = (
        get_settings().latency_arb_edge_threshold
        if edge_threshold is None
        else float(edge_threshold)
    )
    distance = compute_distance(current_price, window_open)
    t_remaining = compute_t_remaining(market.window_start_epoch, window_minutes, now)
    if t_remaining <= 0.0:
        return None
    realized = realized_volatility(kline_closes, window_minutes)
    p_market = market.up_price
    implied = implied_volatility(p_market, t_remaining)
    sigma = conservative_sigma(realized, implied)
    p_model = probability_up(distance, sigma, t_remaining)
    edge = compute_edge(p_model, p_market)
    if edge < threshold:
        return None
    side = "up" if p_model > p_market else "down"
    return Opportunity(
        symbol=symbol.upper(),
        window_minutes=int(window_minutes),
        window_start_epoch=int(market.window_start_epoch),
        side=side,
        p_model=p_model,
        p_market=p_market,
        edge=edge,
        distance=distance,
        t_remaining=t_remaining,
        sigma=sigma,
        current_price=float(current_price),
        window_open=float(window_open),
        market=market,
        detected_at=now,
        feed_lag_ms=feed_lag_ms,
    )


# ────────────── Latency distribution ──────────────

_latency_samples: deque[dict[str, float]] = deque(maxlen=LATENCY_SAMPLES_MAX)


def _record_latency_sample(feed_lag_ms: float, total_ms: float) -> None:
    _latency_samples.append({"feed_lag_ms": float(feed_lag_ms), "total_ms": float(total_ms)})


def _percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * percentile_value / 100.0), len(ordered) - 1)
    return ordered[index]


def latency_stats() -> dict[str, Any]:
    """Feed→order latency distribution summary (p50/p95, ms)."""
    feed_lags = [sample["feed_lag_ms"] for sample in _latency_samples]
    totals = [sample["total_ms"] for sample in _latency_samples]
    return {
        "samples": len(_latency_samples),
        "feed_lag_p50_ms": round(_percentile(feed_lags, 50), 1),
        "feed_lag_p95_ms": round(_percentile(feed_lags, 95), 1),
        "total_p50_ms": round(_percentile(totals, 50), 1),
        "total_p95_ms": round(_percentile(totals, 95), 1),
    }


def _log_latency_distribution() -> None:
    """Log the feed→order latency distribution (the edge only
    exists when Polymarket's lag dominates Binance's)."""
    if not _latency_samples:
        return
    stats = latency_stats()
    logger.info(
        "Latency-arb feed→order latency (n=%s): feed lag p50=%.0fms "
        "p95=%.0fms; total p50=%.0fms p95=%.0fms",
        stats["samples"],
        stats["feed_lag_p50_ms"],
        stats["feed_lag_p95_ms"],
        stats["total_p50_ms"],
        stats["total_p95_ms"],
    )


# ────────────── Opportunities board state ──────────────

_opportunities: list[dict[str, Any]] = []
_opportunities_at: float = 0.0


def _store_opportunities(opportunities: list[Opportunity]) -> None:
    """Persist the last cycle's opportunities (memory + cache)."""
    global _opportunities, _opportunities_at
    _opportunities = [opportunity.to_dict() for opportunity in opportunities]
    _opportunities_at = datetime.now(UTC).timestamp()
    try:
        get_cache("latency_arb").set(
            "opportunities",
            _opportunities,
            ttl_seconds=OPPORTUNITIES_TTL_SECONDS,
        )
    except Exception:
        logger.debug("Opportunity cache write failed", exc_info=True)


def get_latest_opportunities() -> list[dict[str, Any]]:
    """Opportunities from the last engine cycle."""
    try:
        cached = get_cache("latency_arb").get("opportunities")
        if isinstance(cached, list):
            return cached
    except Exception:
        logger.debug("Opportunity cache read failed", exc_info=True)
    return _opportunities


def get_engine_status() -> dict[str, Any]:
    """Engine status for the opportunities endpoint."""
    settings = get_settings()
    return {
        "running": _running,
        "live_mode": bool(settings.latency_arb_live and settings.live_latency_arb_enabled),
        "last_cycle_at": (
            datetime.fromtimestamp(_opportunities_at, tz=UTC).isoformat()
            if _opportunities_at
            else None
        ),
        "cycle_seconds": settings.latency_arb_cycle_seconds,
        "symbols": _engine_symbols(),
        "windows": _engine_windows(),
    }


# ────────────── Configuration ──────────────


def _engine_symbols() -> list[str]:
    raw = get_settings().latency_arb_symbols
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _engine_windows() -> list[int]:
    raw = get_settings().latency_arb_windows
    return [int(item) for item in raw.split(",") if item.strip().isdigit()]


def _default_config(user_id: int) -> LatencyArbConfig:
    """Transient config row with engine defaults.

    Column defaults only apply at flush time, so a transient
    row is constructed with explicit values (mirrors plan 07's
    WhaleConfig handling).
    """
    settings = get_settings()
    return LatencyArbConfig(
        user_id=user_id,
        enabled=False,
        edge_threshold=settings.latency_arb_edge_threshold,
        max_notional=settings.latency_arb_max_notional,
        symbols=list(_engine_symbols()),
        windows=list(_engine_windows()),
        late_entry=settings.latency_arb_late_entry,
        daily_loss_limit=settings.latency_arb_daily_loss_limit,
        alert_on_opportunity=True,
    )


def get_config(user_id: int, db: Session) -> LatencyArbConfig:
    """Return the user's latency-arb config (defaults when unset)."""
    config = TradingBotRepository(db).find_latency_arb_config(user_id)
    return config if config is not None else _default_config(user_id)


def get_or_create_config(user_id: int, db: Session) -> LatencyArbConfig:
    """Return the user's config, staging a default row when unset.

    The staged row is only persisted by the caller's commit, so
    read-only callers should use ``get_config`` instead.
    """
    config = TradingBotRepository(db).find_latency_arb_config(user_id)
    if config is not None:
        return config
    config = _default_config(user_id)
    TradingBotRepository(db).add(config, flush=False)
    return config


def update_user_config(
    user_id: int,
    db: Session,
    changes: dict[str, Any],
) -> LatencyArbConfig:
    """Apply validated API config changes and persist the result."""
    config = get_or_create_config(user_id, db)
    symbols = changes.get("symbols")
    windows = changes.get("windows")
    for field, value in changes.items():
        if field in {"symbols", "windows"}:
            continue
        setattr(config, field, value)
    if symbols is not None:
        config.symbols = [
            item.strip().upper()
            for item in symbols
            if item.strip().upper() in crypto_markets_service.SYMBOLS
        ]
    if windows is not None:
        config.windows = [
            int(item) for item in windows if int(item) in crypto_markets_service.WINDOW_MINUTES
        ]
    return TradingBotRepository(db).save_latency_arb_config(config, refresh=True)


def get_trade_page(
    user_id: int, *, offset: int = 0, limit: int = 50, db: Session
) -> tuple[list[UserTrade], int]:
    """Return one page of this user's latency-arbitrage trade history."""
    repository = TradingOrderRepository(db)
    total = repository.count_latency_arb_trades(user_id, STRATEGY_SOURCE)
    rows = repository.list_latency_arb_trades(
        user_id,
        STRATEGY_SOURCE,
        offset,
        limit,
    )
    return rows, total


# ────────────── Risk gates ──────────────


def _strategy_daily_loss(db: Session, user_id: int) -> float:
    """Sum of negative PnL from latency_arb trades today (UTC)."""
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return abs(
        TradingOrderRepository(db).sum_strategy_daily_loss(
            user_id,
            STRATEGY_SOURCE,
            today_start,
        )
    )


def _circuit_breaker_state(
    db: Session,
    user_id: int,
    now: datetime,
) -> dict[str, Any]:
    """Consecutive-loss circuit breaker state.

    Pauses trading after ``latency_arb_circuit_breaker_losses``
    consecutive losses and resumes
    ``latency_arb_circuit_breaker_resume_seconds`` after the most
    recent loss.
    """
    settings = get_settings()
    limit = settings.latency_arb_circuit_breaker_losses
    rows = TradingOrderRepository(db).list_recent_strategy_trades(
        user_id,
        STRATEGY_SOURCE,
        max(limit, 1),
    )
    consecutive = 0
    last_loss_at: datetime | None = None
    for row in rows:
        if (row.pnl or 0.0) < 0.0:
            consecutive += 1
            if last_loss_at is None:
                last_loss_at = row.created_at or row.updated_at
        else:
            break
    if consecutive >= limit and last_loss_at is not None:
        resume_at = last_loss_at + timedelta(
            seconds=settings.latency_arb_circuit_breaker_resume_seconds
        )
        if now < resume_at:
            return {
                "open": True,
                "consecutive_losses": consecutive,
                "resume_in_seconds": int((resume_at - now).total_seconds()),
            }
    return {"open": False, "consecutive_losses": consecutive, "resume_in_seconds": 0}


# ────────────── Execution ──────────────


async def _fetch_book(condition_id, token_id, cash, max_spread):
    from app.services.trading.latency_book import fetch_executable_book

    return await fetch_executable_book(condition_id, token_id, cash, max_spread)


_execution_outcome_cache = get_cache("latency_arb_outcomes")


def _record_execution_outcome(user_id, opportunity, result):
    from app.utils.metrics import counter

    event = {
        "condition_id": opportunity.market.condition_id,
        "token_id": (
            opportunity.market.up_token_id
            if opportunity.side == "up"
            else opportunity.market.down_token_id
        ),
        "status": result.get("status", "rejected"),
        "reason": result.get("reason", "EXECUTION_RESULT"),
        "at": datetime.now(UTC).isoformat(),
        "source": "polymarket-clob-rest-v1/binary-book-v1",
    }
    if result.get("evidence"):
        event["evidence"] = result["evidence"]
    logger.info("latency_execution_outcome %s", json.dumps(event))
    # Bounded, controlled reason codes only; no provider exception text or secrets.
    reason = event["reason"]
    if isinstance(reason, str) and reason.isupper() and reason.replace("_", "").isalnum():
        counter(f"latency_book_{reason.lower()}_total").inc()
    try:
        _execution_outcome_cache.set(f"outcome:{user_id}", event, ttl_seconds=3600)
    except Exception:
        logger.warning("Latency outcome cache unavailable")


def get_execution_outcome(user_id):
    try:
        return _execution_outcome_cache.get(f"outcome:{user_id}")
    except Exception:
        return None


async def _place_fok_order(
    private_key: str,
    clob_creds: dict | None,
    proxy_address: str | None,
    token_id: str,
    price: float,
    size: float,
) -> dict[str, Any]:
    """Place a FOK order on the CLOB (live mode only).

    Runs the blocking py-clob-client calls in a worker thread so
    the event loop is never blocked. Returns the exchange
    response dict.
    """
    from app.services.trading.live_trading_policy import (
        LiveTradingBlocked,
        block_legacy_live_execution,
    )

    try:
        block_legacy_live_execution("latency_arb")
    except LiveTradingBlocked as exc:
        return {"success": False, "status": "blocked", "errorMsg": str(exc)}

    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    def _place() -> dict[str, Any]:
        client = build_clob_client(private_key, clob_creds, proxy_address)
        # USDC notional → shares, rounded up to 2 decimals so the
        # order never lands below the $1 minimum notional.
        shares = math.ceil((size / price) * 100.0) / 100.0
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 6),
            size=shares,
            side=BUY,
        )
        signed_order = client.create_order(order_args)
        response = client.post_order(signed_order, OrderType.FOK)
        return response if isinstance(response, dict) else {}

    return await asyncio.to_thread(_place)


def _calculation_details(opportunity: Opportunity, extra: dict[str, Any]) -> str:
    """JSON audit blob stored on the UserTrade row."""
    details = {
        "strategy": STRATEGY_SOURCE,
        "symbol": opportunity.symbol,
        "window_minutes": opportunity.window_minutes,
        "window_start_epoch": opportunity.window_start_epoch,
        "window_open": opportunity.window_open,
        "current_price": opportunity.current_price,
        "distance": round(opportunity.distance, 6),
        "t_remaining": round(opportunity.t_remaining, 4),
        "sigma": round(opportunity.sigma, 6),
        "p_model": round(opportunity.p_model, 4),
        "p_market": round(opportunity.p_market, 4),
        "edge": round(opportunity.edge, 4),
        "side": opportunity.side,
        "feed_lag_ms": (
            round(opportunity.feed_lag_ms, 1) if opportunity.feed_lag_ms is not None else None
        ),
    }
    details.update(extra)
    return json.dumps(details)


async def _execute_paper(
    db: Session,
    settings: UserSettings,
    opportunity: Opportunity,
    token_id: str,
    reference_price: float,  # noqa: ARG001 - retained strategy adapter signature
    size: float,
    feed_to_order_ms: float,  # noqa: ARG001 - retained strategy adapter signature
) -> dict[str, Any]:
    """Book only captured native depth against the persistent paper account."""
    from app.services.analytics.paper_evidence import refresh_evidence

    await refresh_evidence(db, opportunity.market.condition_id)
    from app.services.analytics.paper_accounting import submit_paper_order

    result = submit_paper_order(
        db,
        user_id=settings.user_id,
        token_id=token_id,
        side="BUY",
        amount=Decimal(str(size)),
        strategy=STRATEGY_SOURCE,
        intent_key=f"latency:{opportunity.market.condition_id}:{opportunity.window_start_epoch}",
    )
    db.commit()
    return {
        **result,
        "fill_status": result["status"],
        "status": "simulated" if result["status"] in {"filled", "partial"} else "unfilled",
        "size": float(result["notional_usdc"]),
    }


async def _execute_live(
    db: Session,
    user: User,
    settings: UserSettings,  # noqa: ARG001 - shared runner signature
    opportunity: Opportunity,
    token_id: str,
    reference_price: float,  # noqa: ARG001 - Gamma is indicative only
    size: float,
    feed_to_order_ms: float,  # noqa: ARG001 - shared runner signature
    *,
    evidence=None,
) -> dict[str, Any]:
    """Submit a real FOK order through durable execution and shared risk budgets."""
    from app.services.trading.execution.live import place_live_order
    from app.services.trading.latency_book import ExecutableBookEvidence
    from app.services.trading.live_trading_policy import LiveTradingBlocked

    if (
        not isinstance(evidence, ExecutableBookEvidence)
        or evidence.status != "READY"
        or opportunity.side not in ("up", "down")
        or evidence.token_id != token_id
        or evidence.outcome != ("YES" if opportunity.side == "up" else "NO")
        or evidence.condition_id != opportunity.market.condition_id
        or evidence.expires_at is None
        or datetime.now(UTC) >= evidence.expires_at
        or evidence.shares <= 0
        or evidence.worst_case_cash > Decimal(str(size))
    ):
        return {"status": "rejected", "reason": "EXECUTABLE_EVIDENCE_REQUIRED"}
    shares = evidence.shares
    # One economic order per opportunity/window/token survives restarts.
    identity = f"latency:{opportunity.symbol}:{opportunity.window_start_epoch}:{token_id}"
    try:
        result = await run_blocking_execution(
            place_live_order,
            db,
            user_id=user.id,
            token_id=token_id,
            side="BUY",
            price=evidence.limit_price,
            size=shares,
            execution_deadline=evidence.expires_at,
            execution_evidence=evidence.model_dump(mode="json"),
            idempotency_key=identity,
            strategy="latency_arb",
            market_id=opportunity.market.condition_id,
            order_type="FOK",
        )
    except (LiveTradingBlocked, ValueError) as exc:
        return {"status": "rejected", "reason": str(exc)}
    return {
        **result,
        "size": float(evidence.worst_case_cash),
        "evidence": evidence.model_dump(mode="json"),
    }


async def _execute_opportunity(
    db: Session,
    config: LatencyArbConfig,
    settings: UserSettings,
    user: User,
    opportunity: Opportunity,
    now: datetime,
) -> dict[str, Any]:
    """Run every risk gate and execute one opportunity for one user."""
    engine_settings = get_settings()
    simulation = bool(getattr(settings, "simulation_mode", False))

    # Mode gate: live mode requires LATENCY_ARB_LIVE AND
    # non-simulation user settings.
    if not simulation and not (
        engine_settings.latency_arb_live
        and getattr(engine_settings, "live_latency_arb_enabled", False)
    ):
        return {"status": "skipped", "reason": "live mode disabled"}

    # Latency budget: abort when the feed→order path is too slow.
    decision_ms = (now - opportunity.detected_at).total_seconds() * 1000.0
    feed_lag = opportunity.feed_lag_ms or 0.0
    feed_to_order_ms = feed_lag + decision_ms
    if feed_to_order_ms > engine_settings.latency_arb_max_latency_ms:
        logger.info(
            "Latency-arb abort: feed→order %.0fms exceeds %dms budget",
            feed_to_order_ms,
            engine_settings.latency_arb_max_latency_ms,
        )
        _record_latency_sample(feed_lag, feed_to_order_ms)
        return {
            "status": "aborted",
            "reason": "latency budget exceeded",
            "feed_to_order_ms": round(feed_to_order_ms, 1),
        }

    # Size: per-user cap and the global max notional.
    size = min(float(config.max_notional), engine_settings.latency_arb_max_notional)
    if size <= 0.0:
        return {"status": "rejected", "reason": "zero trade size"}

    # Shared pre-trade gate (plan 01): halt / loss limits / drawdown /
    # position caps / daily loss.
    if not _PRE_TRADE_GATE_AVAILABLE or apply_global_safety_caps is None:
        return {"status": "rejected", "reason": "pre-trade gate unavailable"}
    size, rejection = apply_global_safety_caps(settings, size, db)
    if rejection:
        return {"status": "rejected", "reason": rejection}

    # Per-strategy daily loss limit.
    daily_loss = _strategy_daily_loss(db, settings.user_id)
    if daily_loss >= float(config.daily_loss_limit):
        return {
            "status": "rejected",
            "reason": "strategy daily loss limit reached",
            "daily_loss": round(daily_loss, 2),
        }

    # Consecutive-loss circuit breaker.
    breaker = _circuit_breaker_state(db, settings.user_id, now)
    if breaker["open"]:
        return {
            "status": "rejected",
            "reason": (
                "circuit breaker open "
                f"({breaker['consecutive_losses']} consecutive losses, "
                f"resumes in {breaker['resume_in_seconds']}s)"
            ),
        }

    token_id = (
        opportunity.market.up_token_id
        if opportunity.side == "up"
        else opportunity.market.down_token_id
    )
    reference_price = (
        opportunity.market.up_price if opportunity.side == "up" else opportunity.market.down_price
    )
    if opportunity.side not in ("up", "down") or not token_id or reference_price <= 0.0:
        return {"status": "rejected", "reason": "market data incomplete"}

    evidence = None
    if not simulation:
        from app.services.trading.latency_book import ExecutableBookEvidence

        if not binance_ws_service.is_connected():
            return {"status": "rejected", "reason": "FEED_DISCONNECTED"}
        evidence = await _fetch_book(
            opportunity.market.condition_id, token_id, size, engine_settings.latency_arb_max_spread
        )
        if not isinstance(evidence, ExecutableBookEvidence):
            return {"status": "rejected", "reason": "BOOK_EVIDENCE_UNAVAILABLE"}
        if evidence.status != "READY":
            return {
                "status": "rejected",
                "reason": evidence.reason,
                "evidence": evidence.model_dump(mode="json"),
            }
        if evidence.outcome != ("YES" if opportunity.side == "up" else "NO"):
            return {"status": "rejected", "reason": "TOKEN_OUTCOME_MISMATCH"}
        # Include awaited metadata/fee/book requests in the feed-to-submit budget.
        completed_at = datetime.now(UTC)
        if evidence.expires_at is None or completed_at >= evidence.expires_at:
            return {"status": "rejected", "reason": "STALE_BOOK"}
        if not binance_ws_service.is_connected():
            return {"status": "rejected", "reason": "FEED_DISCONNECTED"}
        if (
            opportunity.feed_lag_ms is None
            or not math.isfinite(opportunity.feed_lag_ms)
            or opportunity.feed_lag_ms < 0
            or completed_at < opportunity.detected_at
        ):
            return {"status": "rejected", "reason": "FEED_FRESHNESS_UNKNOWN"}
        feed_to_order_ms = (
            feed_lag + (completed_at - opportunity.detected_at).total_seconds() * 1000
        )
        if feed_to_order_ms > engine_settings.latency_arb_max_latency_ms:
            return {"status": "aborted", "reason": "LATENCY_BUDGET_EXCEEDED"}
        deadline = opportunity.detected_at + timedelta(
            milliseconds=engine_settings.latency_arb_max_latency_ms - feed_lag
        )
        evidence = evidence.model_copy(update={"expires_at": min(evidence.expires_at, deadline)})

    if simulation:
        result = await _execute_paper(
            db,
            settings,
            opportunity,
            token_id,
            reference_price,
            size,
            feed_to_order_ms,
        )
    else:
        result = await _execute_live(
            db,
            user,
            settings,
            opportunity,
            token_id,
            reference_price,
            size,
            feed_to_order_ms,
            evidence=evidence,
        )

    _record_latency_sample(feed_lag, feed_to_order_ms)
    return result


def _dispatch_opportunity_alert(
    user_id: int,
    opportunity: Opportunity,
    result: dict[str, Any],
) -> None:
    """Dispatch a latency_arb_opportunity alert (plan 09)."""
    try:
        from app.services.users.alert_service import dispatch
    except ImportError:
        logger.debug("alert_service unavailable — latency arb alert skipped")
        return

    async def _send() -> None:
        payload = {
            "symbol": opportunity.symbol,
            "window_minutes": opportunity.window_minutes,
            "side": opportunity.side,
            "p_model": round(opportunity.p_model, 4),
            "p_market": round(opportunity.p_market, 4),
            "edge": round(opportunity.edge, 4),
            "condition_id": opportunity.market.condition_id,
            "question": opportunity.market.question,
            "status": result.get("status"),
            "message": (
                f"{opportunity.symbol} {opportunity.window_minutes}m "
                f"{opportunity.side} edge {opportunity.edge * 100.0:.1f}% "
                f"(model {opportunity.p_model:.2f} vs market "
                f"{opportunity.p_market:.2f})"
            ),
        }
        await dispatch(
            "latency_arb_opportunity",
            user_id,
            payload,
            background=True,
        )

    try:
        asyncio.get_running_loop().create_task(_send())
    except Exception:
        logger.debug("Latency arb alert dispatch failed", exc_info=True)


# ────────────── Engine cycle ──────────────


async def _fetch_opportunities(now: datetime) -> list[Opportunity]:
    """Evaluate every enabled symbol/window for the current cycle."""
    opportunities: list[Opportunity] = []
    late_entry = get_settings().latency_arb_late_entry
    late_entry_seconds = get_settings().latency_arb_late_entry_seconds
    for symbol in _engine_symbols():
        for window_minutes in _engine_windows():
            market = crypto_markets_service.get_market(symbol, window_minutes)
            if market is None:
                continue
            # Wrong-window guard: never trade a market whose window
            # start is not the current UTC window.
            if not crypto_markets_service.window_start_matches(market, window_minutes, now):
                continue
            feed_symbol = f"{symbol}USDT"
            current_price = binance_ws_service.get_last_price(feed_symbol)
            window_open = binance_ws_service.get_window_open_price(
                feed_symbol, market.window_start_epoch
            )
            if current_price is None or not window_open or window_open <= 0.0:
                continue
            t_remaining = compute_t_remaining(market.window_start_epoch, window_minutes, now)
            # Late-entry halt: stop trading the final seconds of a
            # window unless explicitly enabled.
            remaining_seconds = t_remaining * window_minutes * 60.0
            if remaining_seconds <= late_entry_seconds and not late_entry:
                continue
            kline_closes = binance_ws_service.get_recent_closes(feed_symbol)
            feed_lag_ms = binance_ws_service.get_feed_lag_ms(feed_symbol)
            opportunity = evaluate_opportunity(
                symbol,
                window_minutes,
                market,
                current_price,
                window_open,
                now,
                kline_closes,
                feed_lag_ms,
            )
            if opportunity is not None:
                opportunities.append(opportunity)
    return opportunities


def _enabled_configs(db: Session) -> list[tuple[LatencyArbConfig, UserSettings, User]]:
    """All users with latency arb enabled, with their settings and user row."""
    try:
        return TradingBotRepository(db).list_enabled_latency_arb_accounts()
    except (KeyError, TypeError, AttributeError):
        pass
    configs = db.query(LatencyArbConfig).filter(LatencyArbConfig.enabled.is_(True)).all()
    enabled: list[tuple[LatencyArbConfig, UserSettings, User]] = []
    for config in configs:
        settings = db.query(UserSettings).filter(UserSettings.user_id == config.user_id).first()
        user = db.query(User).filter(User.id == config.user_id).first()
        if settings is None or user is None:
            continue
        enabled.append((config, settings, user))
    return enabled


async def _execute_for_users(
    opportunities: list[Opportunity],
    now: datetime,
) -> int:
    """Execute eligible opportunities for every enabled user."""
    if not opportunities:
        return 0
    db = SessionLocal()
    executed = 0
    try:
        for config, settings, user in _enabled_configs(db):
            for opportunity in opportunities:
                if opportunity.edge < float(config.edge_threshold):
                    continue
                if config.symbols and opportunity.symbol not in [
                    item.upper() for item in config.symbols
                ]:
                    continue
                if config.windows and opportunity.window_minutes not in [
                    int(item) for item in config.windows
                ]:
                    continue
                result = await _execute_opportunity(db, config, settings, user, opportunity, now)
                _record_execution_outcome(settings.user_id, opportunity, result)
                if result.get("status") in ("simulated", "submitted"):
                    executed += 1
                    if bool(config.alert_on_opportunity):
                        _dispatch_opportunity_alert(settings.user_id, opportunity, result)
    except Exception:
        logger.warning("Latency-arb execution cycle failed", exc_info=True)
        db.rollback()
    finally:
        db.close()
    return executed


def _parse_calculation_details(trade: UserTrade) -> dict[str, Any]:
    if not trade.calculation_details:
        return {}
    try:
        parsed = json.loads(trade.calculation_details)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _settle_expired_trades(now: datetime) -> int:
    """Only native, finalized, rule-version-matched evidence can settle paper shares.

    Binance window prices are signal inputs and cannot establish Polymarket payouts.
    Legacy rows remain unverified instead of receiving invented realized returns.
    """
    from app.models.trading.paper_account import PaperAccount
    from app.services.analytics.paper_accounting import settle_captured

    if not get_settings().paper_journal_v2_enabled:
        return 0
    db = SessionLocal()
    try:
        count = 0
        accounts = db.query(PaperAccount).all()
        for account in accounts:
            count += settle_captured(db, account.user_id, account.account_id, now)
        if count:
            db.commit()
        return count
    except Exception:
        logger.warning("Native paper settlement failed", exc_info=True)
        db.rollback()
        return 0
    finally:
        db.close()


async def _engine_cycle() -> None:
    """One evaluation cycle: discover, evaluate, execute, settle."""
    now = datetime.now(UTC)
    if crypto_markets_service.markets_stale():
        await crypto_markets_service.refresh_markets()
    opportunities = await _fetch_opportunities(now)
    _store_opportunities(opportunities)
    _log_latency_distribution()
    await _execute_for_users(opportunities, now)
    await _settle_expired_trades(now)


async def _engine_loop(stop_event: asyncio.Event) -> None:
    """Engine main loop (scheduler-locked)."""
    while not stop_event.is_set():
        try:
            await _engine_cycle()
        except Exception:
            logger.warning("Latency-arb engine cycle failed", exc_info=True)
        scheduler_heartbeat("latency_arb")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=get_settings().latency_arb_cycle_seconds,
            )


# ────────────── Lifecycle ──────────────

_engine_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_running = False


async def start_latency_arb_engine() -> None:
    """Start the latency-arbitrage engine as a background task.

    Scheduler-locked ("latency_arb"): a second worker process
    fails to acquire the lock and skips starting its loop. Owns
    the Binance feed, the market-discovery refresh and the engine
    loop.
    """
    global _engine_task, _stop_event, _running
    if _running:
        return
    if not acquire_scheduler_lock("latency_arb"):
        logger.info("Latency arb engine: scheduler lock held elsewhere; not starting")
        return
    _stop_event = asyncio.Event()
    _running = True
    await binance_ws_service.start_binance_feed()
    await crypto_markets_service.start_crypto_markets_refresh()
    _engine_task = asyncio.create_task(_engine_loop(_stop_event))
    logger.info(
        "Latency arbitrage engine started (live_mode=%s)",
        get_settings().latency_arb_live,
    )


async def stop_latency_arb_engine() -> None:
    """Stop the engine and release the scheduler lock."""
    global _engine_task, _stop_event, _running
    if not _running:
        return
    _running = False
    if _stop_event is not None:
        _stop_event.set()
    if _engine_task is not None and not _engine_task.done():
        _engine_task.cancel()
        try:
            await _engine_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Latency arb engine stop error: %s", exc)
    await crypto_markets_service.stop_crypto_markets_refresh()
    await binance_ws_service.stop_binance_feed()
    _engine_task = None
    _stop_event = None
    release_scheduler_lock("latency_arb")
    logger.info("Latency arbitrage engine stopped")
