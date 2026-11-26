"""
Arbitrage Detection Service (read-only scanner).

Scans Polymarket markets for *indicative* price signals:
  1. ``complement_signal``: indicative Gamma ``outcomePrices`` sum below 1.00.
     The gap is a gross indicative price gap in percentage points. It is not
     executable, not fee-adjusted and not a return on capital.
  2. ``spread_signal``: a wide quoted bid/ask spread. This is a maker/liquidity
     research signal and is not arbitrage.

Every record carries ``schema_version`` ``arb-scan-v2`` and explicit
``INDICATIVE_ONLY`` / ``NOT_EXECUTABLE`` / ``UNKNOWN_FEES`` badges. The legacy
``profit_pct`` field is kept as a deprecated alias with its original numeric
meaning so existing clients keep working; new clients read
``gross_price_gap_pct_points``. Executable feasibility lives in
``app.services.analytics.arbitrage`` (payoff-verified, depth-aware, paper only).

Runs as a background task, logging signals for user review.
"""

import asyncio
import contextlib
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.utils.scheduler_lock import (
    acquire_scheduler_lock,
    release_scheduler_lock,
    scheduler_heartbeat,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

_arb_task: asyncio.Task | None = None
_arb_opportunities: list[dict[str, Any]] = []
ARB_CHECK_INTERVAL = 120  # 2 minutes
ARB_API_CONCURRENCY = 10
SCAN_SCHEMA_VERSION = "arb-scan-v2"
INDICATIVE_BADGES = ("INDICATIVE_ONLY", "NOT_EXECUTABLE", "UNKNOWN_FEES")
COMPLEMENT_LABEL = "Indicative price gap (not guaranteed profit)"
SPREAD_LABEL = "Wide spread (liquidity signal, not arbitrage)"
LEGACY_FIELD_NOTE = (
    "profit_pct is a deprecated alias of gross_price_gap_pct_points: an indicative gross "
    "price gap in percentage points, not a return on capital and not executable."
)
MAX_CANDIDATES = 200
_arb_candidates: dict[str, dict[str, Any]] = {}
_arb_semaphore = asyncio.Semaphore(ARB_API_CONCURRENCY)


async def _fetch_markets_with_rate_limit(service: Any) -> list[dict[str, Any]]:
    async with _arb_semaphore:
        return await service.get_active_markets(limit=100)


def _check_complement_arbitrage(market: dict[str, Any]) -> dict[str, Any] | None:
    """
    Flag an indicative complement price gap: Yes + No outcome prices sum below 0.98.
    Uses outcomePrices from get_active_markets (list of 2 price strings).
    Example: Yes = 0.45, No = 0.50 -> sum 0.95 -> gross indicative gap of 5.0 points.
    Prices are indicative midpoints, not fills: depth, fees and settlement rules are
    unverified, so the record is never executable on its own.
    """
    outcome_prices = market.get("outcomePrices", [])
    if not isinstance(outcome_prices, list) or len(outcome_prices) < 2:
        return None

    try:
        prices = [float(p) for p in outcome_prices]
    except (ValueError, TypeError):
        return None

    if any(p <= 0 for p in prices):
        return None

    total = sum(prices)
    if total < 0.98:  # indicative threshold, not a fee-adjusted edge
        gap = round((1.0 - total) * 100, 2)
        return {
            "type": "complement",
            "signal_class": "complement_signal",
            "schema_version": SCAN_SCHEMA_VERSION,
            "label": COMPLEMENT_LABEL,
            "badges": list(INDICATIVE_BADGES),
            "market_id": market.get("condition_id", market.get("conditionId", "")),
            "market_title": market.get("question", market.get("_event_title", "Unknown")),
            "prices": prices,
            "total_cost": round(total, 4),
            "gross_price_gap_pct_points": gap,
            "profit_pct": gap,
            "deprecated_fields": {"profit_pct": LEGACY_FIELD_NOTE},
            "detected_at": utc_now().isoformat(),
        }
    return None


def _check_spread_arbitrage(market: dict[str, Any]) -> dict[str, Any] | None:
    """
    Flag unusually large bid-ask spreads (>=5%) as a liquidity research signal.
    A wide spread alone is not arbitrage: nothing is locked in and no fill is implied.
    Uses bestBid/bestAsk from get_active_markets.
    """
    bid_raw = market.get("bestBid")
    ask_raw = market.get("bestAsk")
    if bid_raw is None or ask_raw is None:
        return None

    try:
        bid = float(bid_raw)
        ask = float(ask_raw)
    except (ValueError, TypeError):
        return None

    if bid > 0 and ask > 0 and ask > bid:
        spread = ask - bid
        spread_pct = (spread / ask) * 100
        if spread_pct >= 5.0:
            return {
                "type": "spread",
                "signal_class": "spread_signal",
                "schema_version": SCAN_SCHEMA_VERSION,
                "label": SPREAD_LABEL,
                "badges": [*INDICATIVE_BADGES, "LIQUIDITY_SIGNAL_NOT_ARBITRAGE"],
                "market_id": market.get("condition_id", market.get("conditionId", "")),
                "market_title": market.get("question", market.get("_event_title", "Unknown")),
                "token_id": "",
                "outcome": "Yes",
                "bid": bid,
                "ask": ask,
                "spread_pct": round(spread_pct, 2),
                "detected_at": utc_now().isoformat(),
            }
    return None


def record_indicative_candidate(signal: dict[str, Any]) -> None:
    """Keep an INDICATIVE candidate for a complement signal. Never executable."""
    from app.services.analytics.arbitrage.execution_feasibility import indicative_candidate

    condition = str(signal.get("market_id") or "")
    if not condition:
        return
    try:
        candidate = indicative_candidate(
            kind="paired_outcome",
            venue_ids=["polymarket"],
            condition_ids=[condition],
            gross_gap=Decimal(str(signal["gross_price_gap_pct_points"])),
            as_of=datetime.fromisoformat(signal["detected_at"]),
            detail={"prices": [str(p) for p in signal["prices"]]},
        )
    except (ValueError, ArithmeticError):
        return
    record = candidate.model_dump(mode="json")
    record["market_title"] = signal["market_title"]
    record["gross_price_gap_pct_points"] = signal["gross_price_gap_pct_points"]
    record["label"] = COMPLEMENT_LABEL
    _arb_candidates.pop(candidate.candidate_id, None)
    _arb_candidates[candidate.candidate_id] = record
    while len(_arb_candidates) > MAX_CANDIDATES:
        _arb_candidates.pop(next(iter(_arb_candidates)))


def get_recent_candidates() -> list[dict[str, Any]]:
    """Newest-first INDICATIVE candidates from the last scans."""
    return list(reversed(list(_arb_candidates.values())))[:50]


def get_candidate(candidate_id: str) -> dict[str, Any] | None:
    return _arb_candidates.get(candidate_id)


async def scan_for_arbitrage() -> list[dict[str, Any]]:
    """Scan active markets for arbitrage opportunities."""
    from app.services.market_data.polymarket_service import get_polymarket_service

    opportunities: list[dict[str, Any]] = []
    service = get_polymarket_service()

    try:
        markets = await _fetch_markets_with_rate_limit(service)
    except Exception as e:
        logger.error("Failed to fetch markets for arbitrage scan: %s", e)
        return opportunities

    for market in markets:
        comp = _check_complement_arbitrage(market)
        if comp:
            opportunities.append(comp)
            logger.info(
                "ARB SIGNAL [complement, indicative]: %s sum=%.4f gross_gap=%.2f pts",
                comp["market_title"][:50],
                comp["total_cost"],
                comp["gross_price_gap_pct_points"],
            )
            record_indicative_candidate(comp)

        spread = _check_spread_arbitrage(market)
        if spread:
            opportunities.append(spread)
            logger.info(
                "ARB SIGNAL [spread, liquidity only]: %s %s bid=%.4f ask=%.4f spread=%.2f%%",
                spread["market_title"][:50],
                spread["outcome"],
                spread["bid"],
                spread["ask"],
                spread["spread_pct"],
            )

    return opportunities


async def _arb_monitor_loop():
    """Background loop that periodically scans for arbitrage."""
    global _arb_opportunities
    if not acquire_scheduler_lock("arbitrage_monitor"):
        return
    logger.info("Arbitrage monitor started (interval=%ds)", ARB_CHECK_INTERVAL)
    try:
        while True:
            try:
                opps = await scan_for_arbitrage()
                # Keep last 100 opportunities
                _arb_opportunities = (opps + _arb_opportunities)[:100]
                if opps:
                    logger.info("Found %d arbitrage opportunities", len(opps))
            except Exception as e:
                logger.error("Arbitrage scan error: %s", e)
            scheduler_heartbeat("arbitrage_monitor")
            await asyncio.sleep(ARB_CHECK_INTERVAL)
    finally:
        release_scheduler_lock("arbitrage_monitor")


async def start_arbitrage_monitor():
    global _arb_task
    if _arb_task and not _arb_task.done():
        return
    _arb_task = asyncio.create_task(_arb_monitor_loop())
    logger.info("Arbitrage monitor task created")


async def stop_arbitrage_monitor():
    global _arb_task
    if _arb_task and not _arb_task.done():
        _arb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _arb_task
    _arb_task = None
    logger.info("Arbitrage monitor stopped")


def get_recent_opportunities() -> list[dict[str, Any]]:
    """Get recently detected arbitrage opportunities."""
    return _arb_opportunities[:50]
