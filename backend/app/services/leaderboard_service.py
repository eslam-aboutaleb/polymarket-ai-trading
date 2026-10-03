"""
Leaderboard service – fetches top trader data from Polymarket.

Strategy:
1. Scrape the polymarket.com/leaderboard page and extract __NEXT_DATA__ JSON.
   This contains real leaderboard data (volume + profit rankings) for 20 traders.
2. Merge volume + profit datasets so we have both metrics per trader.
3. Persist results in the `winners` table and cache in-memory.
"""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.winner import Winner
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

POLYMARKET_URL = "https://polymarket.com"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"

# Polymarket's public pages reject requests without a browser-like User-Agent.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
POLYMARKET_CLOB_API = "https://clob.polymarket.com"
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"

# In-memory TTL cache (seconds)
LEADERBOARD_CACHE_TTL = 300
_leaderboard_cache: dict[str, Any] = {}
_trader_trades_cache: dict[str, Any] = {}  # wallet -> {data, ts}
TRADER_TRADES_CACHE_TTL = 120  # 2 minutes
_profile_stats_cache: dict[str, Any] = {}  # wallet -> {data, ts}
PROFILE_STATS_CACHE_TTL = 120  # 2 minutes


def _parse_next_data(html: str) -> dict | None:
    """Extract __NEXT_DATA__ JSON from a Next.js rendered page."""
    if not html:
        return None
    marker = 'id="__NEXT_DATA__"'
    marker_pos = html.find(marker)
    if marker_pos == -1:
        return None
    script_start = html.rfind("<script", 0, marker_pos)
    if script_start == -1:
        return None
    tag_end = html.find(">", marker_pos)
    if tag_end == -1:
        return None
    script_end = html.find("</script>", tag_end)
    if script_end == -1:
        return None
    payload = html[tag_end + 1 : script_end].strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse __NEXT_DATA__ JSON: %s", e)
        return None


def _extract_leaderboard_from_next_data(next_data: dict) -> dict[str, list[dict]]:
    """
    Walk the dehydrated React-Query cache in __NEXT_DATA__ and pull out
    volume, profit, and biggestWins datasets.

    Query key structures found:
      ["/leaderboard", "volume", "30d", 1, "overall", null]
      ["/leaderboard", "profit", "30d", 1, "overall", null]
      ["/leaderboard", "biggestWins", "30d", 20, "overall"]
    """
    result: dict[str, list[dict]] = {"volume": [], "profit": [], "biggestWins": []}

    try:
        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )
    except (AttributeError, TypeError):
        return result

    for q in queries:
        qk = q.get("queryKey", [])
        if not qk or qk[0] != "/leaderboard":
            continue
        sort_type = qk[1] if len(qk) > 1 else None
        data = q.get("state", {}).get("data", [])
        if sort_type in result and isinstance(data, list):
            result[sort_type] = data

    return result


# Map our period values to Polymarket's v1 API timePeriod parameter
_PERIOD_TO_API: dict[str, str] = {
    "24h": "day",
    "7d": "week",
    "30d": "month",
    "all_time": "all",
}

# Also keep the old HTML path mapping as fallback
_PERIOD_TO_PM_PATH: dict[str, str] = {
    "24h": "weekly",
    "7d": "weekly",
    "30d": "monthly",
    "all_time": "all",
}

# Max entries per API page (enforced by Polymarket)
_API_PAGE_SIZE = 50


def _normalize_api_entry(item: dict, rank: int = 0, period: str = "all_time") -> dict:
    """Normalise a Polymarket v1/leaderboard API entry to our format."""
    wallet = str(item.get("proxyWallet") or "").lower()

    name = item.get("userName") or item.get("name") or item.get("pseudonym")

    # Clean up auto-generated names like "0xAbC...1234-1769439463256"
    if name and re.match(r"^0x[0-9A-Fa-f]", name) and "-" in name:
        name = name[:12] + "..."

    profile_image = item.get("profileImage") or item.get("profileImageOptimized")
    fallback_img = "https://polymarket-upload.s3.us-east-2.amazonaws.com/fallback-image.png"
    if profile_image == fallback_img:
        profile_image = None

    pnl_value = _safe_float(item.get("pnl", 0))
    vol_value = _safe_float(item.get("vol", item.get("volume", 0)))

    return {
        "rank": rank or int(item.get("rank", 0)),
        "address": wallet,
        "display_name": name,
        "profile_image": profile_image,
        "profit_loss": pnl_value,
        "volume": vol_value,
        "pnl_24h": pnl_value if period == "24h" else 0.0,
        "pnl_7d": pnl_value if period in ("24h", "7d") else 0.0,
        "pnl_30d": pnl_value if period == "30d" else 0.0,
        "volume_24h": 0.0,
        "trade_count": 0,
        "markets_traded": 0,
        "win_rate": 0.0,
        "positions_value": 0.0,
        "x_username": item.get("xUsername"),
        "verified_badge": item.get("verifiedBadge", False),
    }


async def _fetch_from_api(limit: int = 50, period: str = "all_time") -> list[dict]:
    """
    Fetch leaderboard data from Polymarket's v1/leaderboard API.
    Supports pagination (max 50 per page) up to the requested limit.
    Returns normalised entries sorted by PnL.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    time_period = _PERIOD_TO_API.get(period, "all")
    all_entries: list[dict] = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        offset = 0
        while offset < limit:
            page_size = min(_API_PAGE_SIZE, limit - offset)
            params = {
                "timePeriod": time_period,
                "orderBy": "PNL",
                "limit": page_size,
                "offset": offset,
                "category": "overall",
            }
            try:
                resp = await client.get(
                    f"{POLYMARKET_DATA_API}/v1/leaderboard",
                    params=params,
                    headers=headers,
                )
                if resp.status_code != 200:
                    logger.error(
                        "Leaderboard API error %d for offset=%d period=%s: %s",
                        resp.status_code,
                        offset,
                        period,
                        resp.text[:200],
                    )
                    break

                data = resp.json()
                if not isinstance(data, list) or len(data) == 0:
                    break

                for item in data:
                    entry = _normalize_api_entry(item, period=period)
                    if entry["address"]:
                        all_entries.append(entry)

                logger.debug(
                    "Leaderboard API page: offset=%d, got %d entries (total %d)",
                    offset,
                    len(data),
                    len(all_entries),
                )

                # If we got fewer than requested, we've reached the end
                if len(data) < page_size:
                    break

                offset += len(data)

            except Exception as e:
                logger.error("Leaderboard API fetch error at offset=%d: %s", offset, e)
                break

    # Also fetch volume rankings and merge for complete data
    if all_entries:
        volume_data = await _fetch_volume_rankings(all_entries, time_period, headers)
        if volume_data:
            vol_by_wallet = {v["address"]: v for v in volume_data}
            for entry in all_entries:
                vol_entry = vol_by_wallet.get(entry["address"])
                if vol_entry and vol_entry.get("volume", 0) > 0:
                    entry["volume"] = vol_entry["volume"]

    # Sort by PnL descending and re-rank
    all_entries.sort(key=lambda e: e.get("profit_loss", 0), reverse=True)
    for idx, e in enumerate(all_entries):
        e["rank"] = idx + 1

    logger.info(
        "Leaderboard API fetch complete: %d entries for period=%s",
        len(all_entries),
        period,
    )
    return all_entries[:limit]


async def _fetch_volume_rankings(
    pnl_entries: list[dict], time_period: str, headers: dict
) -> list[dict]:
    """Fetch volume rankings to supplement PnL data with accurate volume."""
    volume_entries = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        # Fetch first few pages of volume data to get volume for top PnL traders
        offset = 0
        max_vol_fetch = min(len(pnl_entries), 200)  # Don't fetch more volume pages than needed
        while offset < max_vol_fetch:
            page_size = min(_API_PAGE_SIZE, max_vol_fetch - offset)
            try:
                resp = await client.get(
                    f"{POLYMARKET_DATA_API}/v1/leaderboard",
                    params={
                        "timePeriod": time_period,
                        "orderBy": "VOL",
                        "limit": page_size,
                        "offset": offset,
                        "category": "overall",
                    },
                    headers=headers,
                )
                if resp.status_code != 200 or not resp.json():
                    break
                data = resp.json()
                if not isinstance(data, list) or len(data) == 0:
                    break
                for item in data:
                    wallet = str(item.get("proxyWallet", "")).lower()
                    if wallet:
                        volume_entries.append(
                            {
                                "address": wallet,
                                "volume": _safe_float(item.get("vol", 0)),
                            }
                        )
                if len(data) < page_size:
                    break
                offset += len(data)
            except Exception:
                break
    return volume_entries


async def _fetch_from_polymarket_page(
    limit: int = 50, period: str = "all_time"
) -> dict[str, list[dict]]:
    """
    Scrape polymarket.com/leaderboard and extract the __NEXT_DATA__ JSON
    which contains pre-rendered leaderboard data (volume + profit + biggestWins).

    Uses the correct Polymarket URL path for each time period:
      /leaderboard/overall/weekly/profit   → 7d data
      /leaderboard/overall/monthly/profit  → 30d data
      /leaderboard/overall/all/profit      → all-time data
    """
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    pm_path = _PERIOD_TO_PM_PATH.get(period, "all")
    url = f"{POLYMARKET_URL}/leaderboard/overall/{pm_path}/profit"

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.error(
                    "Polymarket leaderboard page returned %d for %s", resp.status_code, url
                )
                return {"volume": [], "profit": [], "biggestWins": []}

            next_data = _parse_next_data(resp.text)
            if not next_data:
                logger.error("Could not extract __NEXT_DATA__ from leaderboard page")
                return {"volume": [], "profit": [], "biggestWins": []}

            datasets = _extract_leaderboard_from_next_data(next_data)
            logger.info(
                "Leaderboard scraped (period=%s, url=%s): %d volume entries, "
                "%d profit entries, %d biggest wins",
                period,
                url,
                len(datasets["volume"]),
                len(datasets["profit"]),
                len(datasets["biggestWins"]),
            )
            return datasets

        except Exception as e:
            logger.error("Failed to scrape Polymarket leaderboard: %s", e)
            return {"volume": [], "profit": [], "biggestWins": []}


def _merge_datasets(
    datasets: dict[str, list[dict]], sort_by: str, limit: int, period: str = "all_time"
) -> list[dict]:
    """
    Merge volume and profit datasets from HTML scraping into a single list.
    Fallback path – used only when the v1/leaderboard API is unavailable.
    """

    def _normalize_entry(item: dict, sort_type: str = "profit", period: str = "all_time") -> dict:
        """Normalise a raw __NEXT_DATA__ entry."""
        wallet = (item.get("proxyWallet") or item.get("address") or "").lower()
        name = item.get("name") or item.get("pseudonym") or item.get("userName")
        if name and re.match(r"^0x[0-9A-Fa-f]", name) and "-" in name:
            name = name[:12] + "..."
        profile_image = item.get("profileImage") or item.get("profileImageOptimized")
        fallback_img = "https://polymarket-upload.s3.us-east-2.amazonaws.com/fallback-image.png"
        if profile_image == fallback_img:
            profile_image = None
        pnl_value = _safe_float(item.get("pnl")) if sort_type == "profit" else 0.0
        return {
            "rank": int(item.get("rank", item.get("winRank", 0))),
            "address": wallet,
            "display_name": name,
            "profile_image": profile_image,
            "profit_loss": pnl_value,
            "volume": _safe_float(item.get("volume", item.get("amount", 0))),
            "pnl_24h": pnl_value if period in ("24h", "7d") else 0.0,
            "pnl_7d": pnl_value if period in ("24h", "7d") else 0.0,
            "pnl_30d": pnl_value if period == "30d" else 0.0,
            "volume_24h": 0.0,
            "trade_count": 0,
            "markets_traded": 0,
            "win_rate": 0.0,
            "positions_value": 0.0,
            "x_username": item.get("xUsername"),
        }

    # Index by wallet address
    by_wallet: dict[str, dict] = {}

    # Process profit entries (has accurate PnL)
    for item in datasets.get("profit", []):
        entry = _normalize_entry(item, sort_type="profit", period=period)
        wallet = entry["address"]
        if wallet:
            by_wallet[wallet] = entry

    # Process volume entries (has accurate volume)
    for item in datasets.get("volume", []):
        entry = _normalize_entry(item, sort_type="volume", period=period)
        wallet = entry["address"]
        if wallet:
            if wallet in by_wallet:
                # Merge: update volume from volume dataset, keep PnL from profit
                by_wallet[wallet]["volume"] = entry["volume"]
                if not by_wallet[wallet].get("display_name"):
                    by_wallet[wallet]["display_name"] = entry["display_name"]
                if not by_wallet[wallet].get("profile_image"):
                    by_wallet[wallet]["profile_image"] = entry["profile_image"]
            else:
                by_wallet[wallet] = entry

    entries = list(by_wallet.values())

    # Sort by PnL (profit_loss is already set to the period-specific value)
    sort_field = "volume" if sort_by == "volume" else "profit_loss"
    entries.sort(key=lambda e: e.get(sort_field, 0), reverse=True)

    # Re-rank
    for idx, e in enumerate(entries):
        e["rank"] = idx + 1

    return entries[:limit]


# ── Per-trader enrichment cache (win rate, markets, positions) ──
_enrichment_cache: dict[str, Any] = {}  # wallet -> {data, ts}
ENRICHMENT_CACHE_TTL = 600  # 10 minutes – heavier to compute


async def _enrich_single_trader(
    client: httpx.AsyncClient, wallet: str, headers: dict
) -> dict[str, Any]:
    """
    Fetch real-time stats for a single trader wallet from Polymarket APIs.
    Returns dict with win_rate, markets_traded, positions_value, position_count.
    """
    now_ts = datetime.now(UTC).timestamp()
    cached = _enrichment_cache.get(wallet)
    if cached and (now_ts - cached["ts"]) < ENRICHMENT_CACHE_TTL:
        return cached["data"]

    result: dict[str, Any] = {
        "win_rate": 0.0,
        "markets_traded": 0,
        "positions_value": 0.0,
        "position_count": 0,
    }

    market_ids: set = set()
    wins = 0
    total_resolved = 0
    market_trades: dict[str, list[dict]] = {}

    # 1. Fetch activity feed (buys, sells, redemptions) – fetch up to 500 for better accuracy
    try:
        resp = await client.get(
            f"{POLYMARKET_DATA_API}/activity",
            params={"user": wallet, "limit": 500},
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for a in data:
                    mid = str(a.get("conditionId", a.get("market", a.get("condition_id", ""))))
                    if mid:
                        market_ids.add(mid)
                        market_trades.setdefault(mid, []).append(a)
    except Exception as e:
        logger.debug("Enrichment activity fetch error for %s: %s", wallet[:10], e)

    # 2. Compute win rate from market-level analysis
    # Polymarket Data API types: TRADE (with side BUY/SELL), REDEEM, MERGE, REWARD
    for _mid, acts in market_trades.items():
        buy_cost = 0.0
        sell_revenue = 0.0
        has_redemption = False
        has_merge = False
        redemption_value = 0.0

        for a in acts:
            act_type = str(a.get("type", "")).upper()
            side = str(a.get("side", "")).upper()
            size = _safe_float(a.get("usdcSize", a.get("value", a.get("amount", 0))))

            if act_type == "TRADE":
                if side == "BUY":
                    buy_cost += size
                elif side == "SELL":
                    sell_revenue += size
            elif act_type == "REDEEM":
                has_redemption = True
                redemption_value += size
            elif act_type == "MERGE":
                has_merge = True

        # REDEEM = winning position resolved in trader's favor
        if has_redemption:
            total_resolved += 1
            wins += 1
        # MERGE with no redeem = trader exited by merging YES+NO (loss/neutral)
        elif has_merge and buy_cost > 0:
            total_resolved += 1
            # Not a win
        # Sold position with profit = win
        elif sell_revenue > 0 and buy_cost > 0:
            total_resolved += 1
            if sell_revenue > buy_cost:
                wins += 1

    if total_resolved > 0:
        result["win_rate"] = round(wins / total_resolved * 100, 1)

    result["markets_traded"] = len(market_ids)

    # 3. Fetch positions value
    try:
        resp = await client.get(
            f"{POLYMARKET_DATA_API}/positions",
            params={"user": wallet},
            headers=headers,
        )
        if resp.status_code == 200:
            data = resp.json()
            positions = data if isinstance(data, list) else data.get("positions", [])
            result["position_count"] = len(positions)
            total_val = sum(
                _safe_float(p.get("currentValue", p.get("value", p.get("size", 0))))
                for p in positions
            )
            result["positions_value"] = round(total_val, 2)
    except Exception as e:
        logger.debug("Enrichment positions fetch error for %s: %s", wallet[:10], e)

    _enrichment_cache[wallet] = {"data": result, "ts": now_ts}
    return result


async def _enrich_entries(entries: list[dict], max_enrich: int = 100) -> list[dict]:
    """
    Enrich leaderboard entries with real-time win_rate, markets_traded,
    and positions_value by fetching data concurrently for each trader.

    Uses a semaphore to limit concurrent requests (avoid overwhelming Polymarket API).
    Only enriches the top `max_enrich` entries to keep response times reasonable.
    """
    if not entries:
        return entries

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    # Only enrich the top N entries
    to_enrich = entries[:max_enrich]
    entries[max_enrich:]

    sem = asyncio.Semaphore(10)  # Max 10 concurrent enrichment requests

    async def _limited_enrich(client, wallet, hdrs):
        async with sem:
            return await _enrich_single_trader(client, wallet, hdrs)

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        tasks = [
            _limited_enrich(client, e["address"], headers) for e in to_enrich if e.get("address")
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    enriched_entries = [e for e in to_enrich if e.get("address")]
    for entry, enrichment in zip(enriched_entries, results, strict=False):
        if isinstance(enrichment, Exception):
            logger.debug("Enrichment failed for %s: %s", entry.get("address", "")[:10], enrichment)
            continue
        if enrichment.get("win_rate", 0) > 0:
            entry["win_rate"] = enrichment["win_rate"]
        if enrichment.get("markets_traded", 0) > 0:
            entry["markets_traded"] = enrichment["markets_traded"]
        if enrichment.get("positions_value", 0) > 0:
            entry["positions_value"] = enrichment["positions_value"]

    return entries  # Return all entries (enriched top + remaining)


def _apply_cached_enrichment(entries: list[dict]) -> list[dict]:
    """Apply any previously-cached enrichment data to entries without making new requests."""
    now_ts = datetime.now(UTC).timestamp()
    for entry in entries:
        wallet = entry.get("address", "")
        cached = _enrichment_cache.get(wallet)
        if cached and (now_ts - cached["ts"]) < ENRICHMENT_CACHE_TTL:
            enrichment = cached["data"]
            if enrichment.get("win_rate", 0) > 0:
                entry["win_rate"] = enrichment["win_rate"]
            if enrichment.get("markets_traded", 0) > 0:
                entry["markets_traded"] = enrichment["markets_traded"]
            if enrichment.get("positions_value", 0) > 0:
                entry["positions_value"] = enrichment["positions_value"]
    return entries


async def _background_enrich_and_cache(
    entries: list[dict], cache_key: str, enrich_count: int
) -> None:
    """Run enrichment in the background and update the leaderboard cache."""
    try:
        enriched = await _enrich_entries(entries, max_enrich=enrich_count)
        _leaderboard_cache[cache_key] = {
            "data": enriched,
            "ts": datetime.now(UTC).timestamp(),
        }
        logger.info(
            "Background enrichment done for %s – %d entries enriched",
            cache_key,
            enrich_count,
        )
    except Exception as e:
        logger.warning("Background enrichment failed for %s: %s", cache_key, e)


async def fetch_leaderboard(
    limit: int = 50,
    period: str = "all_time",
) -> list[dict]:
    """
    Public entry-point: return leaderboard data with in-memory caching.
    Fetches from Polymarket's v1/leaderboard API with pagination support
    for up to 1000 traders.

    Enrichment is non-blocking: base data is returned immediately with any
    cached enrichment applied, while a background task refreshes enrichment
    data for subsequent requests.
    """
    cache_key = f"leaderboard:{period}:{limit}"
    cached = _leaderboard_cache.get(cache_key)
    if cached and (datetime.now(UTC).timestamp() - cached["ts"]) < LEADERBOARD_CACHE_TTL:
        return cached["data"]

    # Primary: use the direct v1/leaderboard API (supports up to 1000+ traders)
    entries = await _fetch_from_api(limit=limit, period=period)

    # Fallback: scrape HTML page if API returns no data
    if not entries:
        logger.warning("v1/leaderboard API returned no data, falling back to HTML scraping")
        datasets = await _fetch_from_polymarket_page(limit=min(limit, 50), period=period)
        entries = _merge_datasets(datasets, sort_by=period, limit=min(limit, 50), period=period)

    if not entries:
        logger.warning("Leaderboard fetch returned 0 entries for period=%s", period)
        return entries

    # Apply any previously-cached enrichment data immediately (no extra latency)
    entries = _apply_cached_enrichment(entries)

    # Cache base data right away so the response is fast
    _leaderboard_cache[cache_key] = {
        "data": entries,
        "ts": datetime.now(UTC).timestamp(),
    }
    logger.info("Leaderboard cached (base): %d entries for period=%s", len(entries), period)

    # Kick off enrichment in the background – subsequent requests will get
    # fully enriched data without blocking the current response
    import copy

    enrich_count = min(100, len(entries))
    bg_entries = copy.deepcopy(entries)
    asyncio.ensure_future(_background_enrich_and_cache(bg_entries, cache_key, enrich_count))

    return entries


def upsert_winners(db: Session, entries: list[dict]) -> int:
    """Persist leaderboard entries into the winners table. Returns count."""
    count = 0
    for e in entries:
        addr = e.get("address", "").lower()
        if not addr or len(addr) < 10:
            continue
        winner = db.query(Winner).filter(Winner.wallet_address == addr).first()
        if winner is None:
            winner = Winner(wallet_address=addr)
            db.add(winner)
        winner.display_name = e.get("display_name") or winner.display_name
        winner.profile_image = e.get("profile_image") or winner.profile_image
        winner.total_pnl = e.get("profit_loss", winner.total_pnl)
        winner.pnl_24h = e.get("pnl_24h", winner.pnl_24h)
        winner.pnl_7d = e.get("pnl_7d", winner.pnl_7d)
        winner.pnl_30d = e.get("pnl_30d", winner.pnl_30d)
        winner.recent_pnl = e.get("pnl_24h", winner.recent_pnl)
        winner.volume = e.get("volume", winner.volume)
        winner.volume_24h = e.get("volume_24h", winner.volume_24h)
        winner.trade_count = e.get("trade_count", winner.trade_count)
        winner.markets_traded = e.get("markets_traded", winner.markets_traded)
        winner.win_rate = e.get("win_rate", winner.win_rate)
        winner.positions_value = e.get("positions_value", winner.positions_value)
        winner.leaderboard_rank = e.get("rank")
        winner.last_updated = utc_now()
        count += 1
    db.commit()
    return count


async def fetch_trader_profile(wallet_address: str) -> dict[str, Any]:
    """
    Fetch a single trader's profile data.
    Uses data-api.polymarket.com/activity for recent trades.
    Also checks if we have cached leaderboard data for this wallet.
    """
    addr = wallet_address.lower()
    profile: dict[str, Any] = {
        "wallet_address": addr,
        "display_name": None,
        "profit_loss": 0.0,
        "volume": 0.0,
        "markets_traded": 0,
        "win_rate": None,
        "positions": [],
        "recent_trades": [],
        "profile_image": None,
    }

    # Check cached leaderboard data for this trader's stats
    for _cache_key, cached in _leaderboard_cache.items():
        for entry in cached.get("data", []):
            if entry.get("address", "").lower() == addr:
                profile["display_name"] = entry.get("display_name")
                profile["profit_loss"] = entry.get("profit_loss", 0)
                profile["volume"] = entry.get("volume", 0)
                profile["markets_traded"] = entry.get("markets_traded", 0)
                profile["win_rate"] = entry.get("win_rate")
                profile["profile_image"] = entry.get("profile_image")
                break

    # Fetch recent activity from Data API
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(
                f"{POLYMARKET_DATA_API}/activity",
                params={"user": addr, "limit": 50},
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    profile["recent_trades"] = data[:50]
        except Exception as e:
            logger.debug("Failed to fetch activity for %s: %s", addr, e)

    return profile


# ────────────── Comprehensive Trade Fetching & Analysis ──────────────


async def _scrape_profile_stats(client: httpx.AsyncClient, wallet: str) -> dict[str, Any]:
    """
    Scrape the Polymarket profile page to extract real stats from __NEXT_DATA__.
    Returns dict with pnl, volume, trades_count, markets_traded, join_date, largest_win.
    """
    now_ts = datetime.now(UTC).timestamp()
    cached = _profile_stats_cache.get(wallet)
    if cached and (now_ts - cached["ts"]) < PROFILE_STATS_CACHE_TTL:
        return dict(cached["data"])

    result: dict[str, Any] = {}
    try:
        resp = await client.get(
            f"{POLYMARKET_URL}/profile/{wallet}",
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        if resp.status_code != 200:
            return result

        next_data = _parse_next_data(resp.text)
        if not next_data:
            return result

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        for q in queries:
            qk = q.get("queryKey", [])
            d = q.get("state", {}).get("data")
            if d is None:
                continue

            qk_str = "/".join(str(x) for x in qk[:3]).lower()

            # /api/profile/volume → {amount, pnl, realized, unrealized}
            if "volume" in qk_str and isinstance(d, dict) and "pnl" in d:
                result["pnl"] = _safe_float(d.get("pnl"))
                result["volume"] = _safe_float(d.get("amount"))

            # user-stats → {trades, largestWin, views, joinDate}
            elif "user-stats" in qk_str and isinstance(d, dict):
                result["trades_count"] = int(_safe_float(d.get("trades", 0)))
                result["largest_win"] = _safe_float(d.get("largestWin"))
                result["join_date"] = d.get("joinDate")

            # /api/profile/marketsTraded → {user, traded}
            elif "marketstraded" in qk_str and isinstance(d, dict):
                result["markets_traded"] = int(_safe_float(d.get("traded", 0)))

        logger.info(
            "Profile scrape for %s: pnl=%.1f, trades=%d, markets=%d",
            wallet[:10],
            result.get("pnl", 0),
            result.get("trades_count", 0),
            result.get("markets_traded", 0),
        )
    except Exception as e:
        logger.debug("Profile scrape error for %s: %s", wallet[:10], e)

    _profile_stats_cache[wallet] = {"data": dict(result), "ts": now_ts}
    return result


async def fetch_trader_trades(wallet_address: str, max_trades: int = 500) -> dict[str, Any]:
    """
    Fetch ALL available trades for a trader from Polymarket APIs.
    Returns comprehensive trade data with computed statistics including real win rate.

    Data sources:
      1. Profile page scrape — real PnL, trade count, markets traded
      2. Data API /activity — activity feed (TRADE/REDEEM/MERGE/REWARD)

    Returns dict with keys: trades, activity, positions, stats, trade_summary, profile_stats
    """
    addr = wallet_address.lower()
    now_ts = datetime.now(UTC).timestamp()

    # Check cache
    cached = _trader_trades_cache.get(addr)
    if cached and (now_ts - cached["ts"]) < TRADER_TRADES_CACHE_TTL:
        logger.info("Trader trades cache hit for %s", addr[:10])
        return cached["data"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    all_activity: list[dict] = []
    profile_stats: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        # ── 1. Scrape profile page for real stats (PnL, trade count, markets) ──
        profile_stats = await _scrape_profile_stats(client, addr)

        # ── 2. Fetch activity from Data API ──
        try:
            resp = await client.get(
                f"{POLYMARKET_DATA_API}/activity",
                params={"user": addr, "limit": max_trades},
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    all_activity = data
                    logger.info("Fetched %d activity entries for %s", len(all_activity), addr[:10])
        except Exception as e:
            logger.debug("Activity fetch error for %s: %s", addr[:10], e)

    # ── 3. Compute real statistics from activity data ──
    stats = _compute_trade_stats([], all_activity, [])

    # ── 4. Override stats with profile page data (more accurate) ──
    if profile_stats.get("pnl"):
        stats["total_pnl"] = round(profile_stats["pnl"], 2)
    if profile_stats.get("volume"):
        stats["total_volume"] = round(profile_stats["volume"], 2)
    if profile_stats.get("trades_count"):
        stats["total_trades"] = profile_stats["trades_count"]
    if profile_stats.get("markets_traded"):
        stats["unique_markets"] = profile_stats["markets_traded"]
    if profile_stats.get("largest_win"):
        stats["largest_win"] = round(profile_stats["largest_win"], 2)
    if profile_stats.get("join_date"):
        stats["first_trade_date"] = profile_stats["join_date"]

    # ── 5. Build trade summary for AI analysis ──
    trade_summary = _build_trade_summary([], all_activity, stats)

    result = {
        "trades": [],
        "activity": all_activity[:max_trades],
        "positions": [],
        "stats": stats,
        "trade_summary": trade_summary,
        "profile_stats": profile_stats,
    }

    # Cache it
    _trader_trades_cache[addr] = {"data": result, "ts": now_ts}

    return result


def _compute_trade_stats(
    trades: list[dict],
    activity: list[dict],
    positions: list[dict],
) -> dict[str, Any]:
    """
    Compute real statistics from actual trade data.

    Polymarket Data API activity types:
      - TRADE  (side=BUY or SELL) — trader bought or sold shares
      - REDEEM — trader redeemed winning shares → this is a WIN
      - MERGE  — trader merged YES+NO tokens back to USDC → exit / loss
      - REWARD — platform reward (ignored for win rate)

    Win rate = redeem_markets / (redeem_markets + merge_markets + sold-at-loss markets)
    """
    stats: dict[str, Any] = {
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate": 0.0,
        "total_volume": 0.0,
        "avg_trade_size": 0.0,
        "largest_trade": 0.0,
        "unique_markets": 0,
        "total_pnl": 0.0,
        "avg_buy_price": 0.0,
        "market_categories": {},
        "trade_frequency": "",
        "active_days": 0,
        "first_trade_date": None,
        "last_trade_date": None,
        "avg_hold_time_hours": 0.0,
        "position_count": len(positions),
    }

    if not trades and not activity:
        return stats

    market_ids: set = set()
    trade_sizes: list[float] = []
    trade_dates: list[str] = []
    buy_prices: list[float] = []

    # ── Analyze CLOB trades (if any) ──
    for t in trades:
        size = _safe_float(t.get("size", t.get("amount", 0)))
        price = _safe_float(t.get("price", 0))
        trade_sizes.append(size * price if price else size)

        market_id = t.get("market", t.get("condition_id", t.get("asset_id", "")))
        if market_id:
            market_ids.add(str(market_id))

        ts = t.get("timestamp", t.get("created_at", t.get("match_time", "")))
        if ts:
            trade_dates.append(str(ts))

        side = str(t.get("side", t.get("type", ""))).upper()
        if side == "BUY" and price:
            buy_prices.append(price)

    # ── Analyze activity entries ──
    # Data API returns: type=TRADE (with side=BUY/SELL), REDEEM, MERGE, REWARD
    market_trades: dict[str, list[dict]] = {}

    for a in activity:
        act_type = str(a.get("type", "")).upper()
        market_id = str(a.get("conditionId", a.get("market", a.get("condition_id", ""))))

        if market_id:
            market_ids.add(market_id)
            market_trades.setdefault(market_id, []).append(a)

        size = _safe_float(a.get("usdcSize", a.get("value", a.get("amount", 0))))
        if size and act_type in ("TRADE", "REDEEM"):
            trade_sizes.append(size)

        ts = a.get("timestamp", a.get("createdAt", a.get("created_at", "")))
        if ts:
            trade_dates.append(str(ts))

        # Track buy prices from TRADE entries
        if act_type == "TRADE":
            side = str(a.get("side", "")).upper()
            price = _safe_float(a.get("price", 0))
            if side == "BUY" and price:
                buy_prices.append(price)

        # Track category from title
        title = str(a.get("title", a.get("question", a.get("marketTitle", "")))).lower()
        if title:
            for cat_name, cat_keywords in [
                (
                    "politics",
                    [
                        "election",
                        "president",
                        "senate",
                        "congress",
                        "trump",
                        "biden",
                        "political",
                        "governor",
                        "vote",
                    ],
                ),
                (
                    "crypto",
                    ["bitcoin", "btc", "ethereum", "eth", "crypto", "token", "defi", "blockchain"],
                ),
                (
                    "sports",
                    [
                        "nba",
                        "nfl",
                        "mlb",
                        "soccer",
                        "football",
                        "tennis",
                        "match",
                        "game",
                        "championship",
                    ],
                ),
                (
                    "economics",
                    ["gdp", "inflation", "fed", "interest rate", "economic", "recession", "jobs"],
                ),
                ("tech", ["ai", "apple", "google", "openai", "tech", "software", "twitter"]),
                ("entertainment", ["oscar", "movie", "award", "grammy", "film"]),
            ]:
                if any(kw in title for kw in cat_keywords):
                    stats["market_categories"][cat_name] = (
                        stats["market_categories"].get(cat_name, 0) + 1
                    )
                    break

    # ── Win/Loss counting from activity ──
    # Per conditionId: REDEEM = WIN, MERGE (no redeem) = LOSS, SELL > BUY = WIN
    total_resolved = 0
    wins = 0
    losses = 0
    total_pnl = 0.0

    for _market_id, market_acts in market_trades.items():
        buy_cost = 0.0
        sell_revenue = 0.0
        has_redemption = False
        has_merge = False
        redemption_value = 0.0

        for a in market_acts:
            act_type = str(a.get("type", "")).upper()
            side = str(a.get("side", "")).upper()
            size = _safe_float(a.get("usdcSize", a.get("value", a.get("amount", 0))))

            if act_type == "TRADE":
                if side == "BUY":
                    buy_cost += size
                elif side == "SELL":
                    sell_revenue += size
            elif act_type == "REDEEM":
                has_redemption = True
                redemption_value += size
            elif act_type == "MERGE":
                has_merge = True

        # REDEEM = market resolved in trader's favor → WIN
        if has_redemption:
            total_resolved += 1
            wins += 1
            if buy_cost > 0:
                total_pnl += (redemption_value + sell_revenue) - buy_cost
        # MERGE with no redeem = exit/loss
        elif has_merge and buy_cost > 0:
            total_resolved += 1
            losses += 1
        # No redeem, no merge: check if trader sold for profit or loss
        elif sell_revenue > 0 and buy_cost > 0:
            total_resolved += 1
            market_pnl = sell_revenue - buy_cost
            total_pnl += market_pnl
            if market_pnl > 0:
                wins += 1
            else:
                losses += 1

    # ── Compute final stats ──
    total_trades_count = max(len(trades), len(activity))
    stats["total_trades"] = total_trades_count
    stats["winning_trades"] = wins
    stats["losing_trades"] = losses
    stats["win_rate"] = round((wins / total_resolved * 100), 1) if total_resolved > 0 else 0.0
    stats["unique_markets"] = len(market_ids)
    stats["total_pnl"] = round(total_pnl, 2)

    if trade_sizes:
        stats["total_volume"] = round(sum(trade_sizes), 2)
        stats["avg_trade_size"] = round(sum(trade_sizes) / len(trade_sizes), 2)
        stats["largest_trade"] = round(max(trade_sizes), 2)

    if buy_prices:
        stats["avg_buy_price"] = round(sum(buy_prices) / len(buy_prices), 3)

    # Date analysis
    parsed_dates = []
    for d in trade_dates:
        try:
            if isinstance(d, (int, float)):
                parsed_dates.append(datetime.fromtimestamp(float(d), tz=UTC))
            elif "T" in str(d):
                parsed_dates.append(datetime.fromisoformat(str(d).replace("Z", "+00:00")))
            else:
                parsed_dates.append(datetime.fromtimestamp(int(d), tz=UTC))
        except (ValueError, TypeError, OSError):
            continue

    if parsed_dates:
        parsed_dates.sort()
        stats["first_trade_date"] = parsed_dates[0].isoformat()
        stats["last_trade_date"] = parsed_dates[-1].isoformat()
        unique_days = len({d.date() for d in parsed_dates})
        stats["active_days"] = unique_days
        if unique_days > 0:
            trades_per_day = total_trades_count / unique_days
            stats["trade_frequency"] = f"{trades_per_day:.1f} trades/day"

    return stats


def _build_trade_summary(
    trades: list[dict],
    activity: list[dict],
    stats: dict[str, Any],
) -> str:
    """
    Build a human-readable trade summary for the AI to analyze.
    Includes key stats and a sample of recent trades.
    """
    lines = []
    lines.append("=== TRADER STATISTICS (COMPUTED FROM REAL DATA) ===")
    lines.append(f"Total Trades: {stats['total_trades']}")
    lines.append(f"Winning Trades: {stats['winning_trades']}")
    lines.append(f"Losing Trades: {stats['losing_trades']}")
    lines.append(f"Win Rate: {stats['win_rate']}%")
    lines.append(f"Total Volume: ${stats['total_volume']:,.2f}")
    lines.append(f"Average Trade Size: ${stats['avg_trade_size']:,.2f}")
    lines.append(f"Largest Trade: ${stats['largest_trade']:,.2f}")
    lines.append(f"Unique Markets Traded: {stats['unique_markets']}")
    lines.append(f"Active Trading Days: {stats['active_days']}")
    lines.append(f"Trade Frequency: {stats['trade_frequency']}")
    lines.append(f"Open Positions: {stats['position_count']}")

    if stats.get("first_trade_date"):
        lines.append(f"First Trade: {stats['first_trade_date'][:10]}")
    if stats.get("last_trade_date"):
        lines.append(f"Last Trade: {stats['last_trade_date'][:10]}")
    if stats.get("avg_buy_price"):
        lines.append(f"Average Buy Price: ${stats['avg_buy_price']}")

    # Market category breakdown
    cats = stats.get("market_categories", {})
    if cats:
        lines.append("\n=== MARKET CATEGORY BREAKDOWN ===")
        sorted_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)
        for cat, count in sorted_cats:
            lines.append(f"  {cat.title()}: {count} trades")

    # Recent trade details (last 50)
    recent = activity[:50] if activity else trades[:50]
    if recent:
        lines.append(f"\n=== RECENT TRADES (last {len(recent)}) ===")
        for i, t in enumerate(recent[:50]):
            act_type = str(t.get("type", t.get("action", t.get("side", "unknown")))).upper()
            title = t.get(
                "title", t.get("question", t.get("marketTitle", t.get("market", "Unknown Market")))
            )
            size = _safe_float(t.get("usdcSize", t.get("value", t.get("size", 0))))
            price = _safe_float(t.get("price", 0))
            outcome = t.get("outcome", t.get("outcomeIndex", ""))
            ts = t.get("timestamp", t.get("createdAt", t.get("created_at", "")))

            trade_line = f"  {i + 1}. [{act_type}] {title}"
            if size:
                trade_line += f" | ${size:,.2f}"
            if price:
                trade_line += f" @ ${price:.3f}"
            if outcome:
                trade_line += f" (outcome: {outcome})"
            if ts:
                trade_line += f" | {str(ts)[:19]}"
            lines.append(trade_line)

    return "\n".join(lines)


def _safe_float(val, default=0.0) -> float:
    """Safely convert to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


async def refresh_leaderboard_background(db: Session | None = None, interval: int = 300):
    """
    Long-running background task: refresh the leaderboard every *interval* seconds
    and persist results.  Intended to run via ``asyncio.create_task`` at startup.
    """
    while True:
        try:
            entries = await fetch_leaderboard(limit=100, period="all_time")
            if entries and db is not None:
                try:
                    upsert_winners(db, entries)
                    logger.info("Leaderboard refreshed: %d entries persisted", len(entries))
                except Exception as db_err:
                    logger.debug("Leaderboard DB persist skipped: %s", db_err)
            elif entries:
                logger.info("Leaderboard refreshed: %d entries cached (no DB)", len(entries))
        except Exception as e:
            logger.error("Leaderboard background refresh error: %s", e)
        await asyncio.sleep(interval)
