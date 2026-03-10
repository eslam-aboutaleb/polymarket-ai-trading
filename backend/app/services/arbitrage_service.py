"""
Arbitrage Detection Service.

Scans Polymarket markets for simple arbitrage opportunities:
  1. Complement Arbitrage: Yes + No prices < 1.00 (guaranteed profit)
  2. Spread Arbitrage: Large bid-ask spreads that can be exploited

Runs as a background task, logging opportunities for user review.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from app.utils.time import utc_now

logger = logging.getLogger(__name__)

_arb_task: Optional[asyncio.Task] = None
_arb_opportunities: List[Dict[str, Any]] = []
ARB_CHECK_INTERVAL = 120  # 2 minutes
ARB_API_CONCURRENCY = 10
_arb_semaphore = asyncio.Semaphore(ARB_API_CONCURRENCY)


async def _fetch_markets_with_rate_limit(service: Any) -> List[Dict[str, Any]]:
    async with _arb_semaphore:
        return await service.get_active_markets(limit=100)


def _check_complement_arbitrage(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Check if Yes + No < 1.00 -> guaranteed profit opportunity.
    Uses outcomePrices from get_active_markets (list of 2 price strings).
    Example: Yes = 0.45, No = 0.50 -> total = 0.95 -> buy both for 0.95, redeem 1.00 = 5% profit.
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
    if total < 0.98:  # At least 2% edge after fees
        profit_pct = round((1.0 - total) * 100, 2)
        return {
            "type": "complement",
            "market_id": market.get("condition_id", market.get("conditionId", "")),
            "market_title": market.get("question", market.get("_event_title", "Unknown")),
            "prices": prices,
            "total_cost": round(total, 4),
            "profit_pct": profit_pct,
            "detected_at": utc_now().isoformat(),
        }
    return None


def _check_spread_arbitrage(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Check for unusually large bid-ask spreads (>5%) that represent
    market-making opportunities.  Uses bestBid/bestAsk from get_active_markets.
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


async def scan_for_arbitrage() -> List[Dict[str, Any]]:
    """Scan active markets for arbitrage opportunities."""
    from app.services.polymarket_service import get_polymarket_service

    opportunities: List[Dict[str, Any]] = []
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
                "ARBITRAGE [complement]: %s — cost=%.4f profit=%.2f%%",
                comp["market_title"][:50], comp["total_cost"], comp["profit_pct"],
            )

        spread = _check_spread_arbitrage(market)
        if spread:
            opportunities.append(spread)
            logger.info(
                "ARBITRAGE [spread]: %s %s — bid=%.4f ask=%.4f spread=%.2f%%",
                spread["market_title"][:50], spread["outcome"],
                spread["bid"], spread["ask"], spread["spread_pct"],
            )

    return opportunities


async def _arb_monitor_loop():
    """Background loop that periodically scans for arbitrage."""
    global _arb_opportunities
    logger.info("Arbitrage monitor started (interval=%ds)", ARB_CHECK_INTERVAL)
    while True:
        try:
            opps = await scan_for_arbitrage()
            # Keep last 100 opportunities
            _arb_opportunities = (opps + _arb_opportunities)[:100]
            if opps:
                logger.info("Found %d arbitrage opportunities", len(opps))
        except Exception as e:
            logger.error("Arbitrage scan error: %s", e)
        await asyncio.sleep(ARB_CHECK_INTERVAL)


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
        try:
            await _arb_task
        except asyncio.CancelledError:
            pass
    _arb_task = None
    logger.info("Arbitrage monitor stopped")


def get_recent_opportunities() -> List[Dict[str, Any]]:
    """Get recently detected arbitrage opportunities."""
    return _arb_opportunities[:50]
