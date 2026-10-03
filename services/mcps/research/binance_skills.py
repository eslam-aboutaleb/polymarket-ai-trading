"""
Binance Skills Hub API client.

Public REST APIs from https://developers.binance.com/en/skills providing:
- Smart Money trading signals (buy/sell signals from professional wallets)
- Crypto market rankings (social hype, trending tokens, smart money inflow, PnL leaderboards)
- Token info (search, real-time market data, K-line candlestick charts)

No authentication required — all endpoints are public.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────

BINANCE_WEB3_BASE = "https://web3.binance.com"
KLINE_BASE = "https://dquery.sintral.io"

COMMON_HEADERS = {
    "Accept-Encoding": "identity",
    "User-Agent": "PolymarketBot/1.0",
}

SUPPORTED_CHAINS = {
    "bsc": "56",
    "solana": "CT_501",
    "base": "8453",
    "ethereum": "1",
}

# In-memory TTL cache: key -> (expires_at_epoch, value)
_cache: dict[str, tuple[float, Any]] = {}
_DEFAULT_TTL = 300  # 5 minutes


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_set(key: str, value: Any, ttl: int = _DEFAULT_TTL) -> None:
    _cache[key] = (time.time() + ttl, value)


# ── HTTP helpers ───────────────────────────────────────────

# One shared client per event loop so connections are pooled across the
# ~14 calls in get_crypto_market_context. The MCP server runs each request
# on its own loop (and test harnesses spin up a fresh loop per case), so
# the client is recreated whenever the running loop changes.
_shared_client: httpx.AsyncClient | None = None
_shared_client_loop: asyncio.AbstractEventLoop | None = None


def _get_shared_client() -> httpx.AsyncClient:
    """Lazily create the shared HTTP client for the current event loop."""
    global _shared_client, _shared_client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _shared_client is None or _shared_client.is_closed or _shared_client_loop is not loop:
        _shared_client = httpx.AsyncClient(timeout=15.0, headers=COMMON_HEADERS)
        _shared_client_loop = loop
    return _shared_client


async def _close_shared_client() -> None:
    """Close the shared HTTP client (called on server shutdown)."""
    global _shared_client, _shared_client_loop
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None
    _shared_client_loop = None


async def _get(url: str, params: dict | None = None, timeout: float = 15.0) -> dict:
    """Perform an async GET request with error handling."""
    try:
        resp = await _get_shared_client().get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        logger.warning(f"Binance API timeout: {url}")
        return {}
    except Exception as exc:
        logger.warning(f"Binance API error ({url}): {exc}")
        return {}


async def _post(url: str, json_body: dict, timeout: float = 15.0) -> dict:
    """Perform an async POST request with error handling."""
    try:
        resp = await _get_shared_client().post(
            url,
            json=json_body,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        logger.warning(f"Binance API timeout: {url}")
        return {}
    except Exception as exc:
        logger.warning(f"Binance API error ({url}): {exc}")
        return {}


# ═══════════════════════════════════════════════════════════
# 1. TRADING SIGNALS — Smart Money buy/sell signals
# ═══════════════════════════════════════════════════════════

SMART_MONEY_SIGNALS_URL = (
    f"{BINANCE_WEB3_BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money"
)


async def get_smart_money_signals(
    chain: str = "solana",
    page: int = 1,
    page_size: int = 50,
    signal_type: str = "",
) -> list[dict[str, Any]]:
    """
    Fetch smart money trading signals.

    Returns list of signals with: ticker, direction (buy/sell), alertPrice,
    currentPrice, maxGain, exitRate, smartMoneyCount, status.
    """
    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "CT_501")
    cache_key = f"smart_signals:{chain_id}:{page}:{page_size}:{signal_type}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _post(
        SMART_MONEY_SIGNALS_URL,
        {
            "smartSignalType": signal_type,
            "page": page,
            "pageSize": min(page_size, 100),
            "chainId": chain_id,
        },
    )

    signals = data.get("data", []) or []
    # Normalize into a cleaner format. The API routinely returns null or
    # numeric strings for these fields, so coerce once here: a single
    # malformed record must not drop the whole section downstream.
    result = []
    for s in signals:
        result.append(
            {
                "signal_id": s.get("signalId"),
                "ticker": s.get("ticker", ""),
                "chain_id": s.get("chainId", ""),
                "contract_address": s.get("contractAddress", ""),
                "direction": s.get("direction") or "",  # buy / sell
                "smart_money_count": float(s.get("smartMoneyCount") or 0),
                "alert_price": float(s.get("alertPrice") or 0),
                "current_price": float(s.get("currentPrice") or 0),
                "highest_price": float(s.get("highestPrice") or 0),
                "max_gain_pct": float(s.get("maxGain") or 0),
                "exit_rate": float(s.get("exitRate") or 0),
                "status": s.get("status", ""),  # active / timeout / completed
                "total_value_usd": float(s.get("totalTokenValue") or 0),
                "signal_time": float(s.get("signalTriggerTime") or 0),
                "signal_count": float(s.get("signalCount") or 0),
                "launch_platform": s.get("launchPlatform", ""),
                "is_alpha": s.get("isAlpha", False),
                "logo_url": _icon_url(s.get("logoUrl", "")),
                "tags": _flatten_tags(s.get("tokenTag", {})),
            }
        )

    _cache_set(cache_key, result)
    return result


async def get_active_buy_signals(chain: str = "solana", min_smart_money: int = 2) -> list[dict]:
    """Get only active BUY signals with minimum smart money count — best for bot decisions."""
    signals = await get_smart_money_signals(chain=chain, page_size=100)
    return [
        s
        for s in signals
        if s["direction"] == "buy"
        and s["status"] == "active"
        and s["smart_money_count"] >= min_smart_money
    ]


# ═══════════════════════════════════════════════════════════
# 2. CRYPTO MARKET RANKINGS
# ═══════════════════════════════════════════════════════════

SOCIAL_HYPE_URL = (
    f"{BINANCE_WEB3_BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse"
    "/social/hype/rank/leaderboard"
)
UNIFIED_RANK_URL = (
    f"{BINANCE_WEB3_BASE}/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse"
    "/unified/rank/list"
)
SMART_MONEY_INFLOW_URL = (
    f"{BINANCE_WEB3_BASE}/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query"
)
PNL_LEADERBOARD_URL = (
    f"{BINANCE_WEB3_BASE}/bapi/defi/v1/public/wallet-direct/market/leaderboard/query"
)


async def get_social_hype_ranking(
    chain: str = "solana",
    time_range: int = 1,
    sentiment: str = "All",
) -> list[dict[str, Any]]:
    """
    Get social hype sentiment leaderboard.
    time_range: 1 = 24 hours.
    Returns tokens ranked by social buzz with sentiment summaries.
    """
    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "CT_501")
    cache_key = f"social_hype:{chain_id}:{time_range}:{sentiment}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _get(
        SOCIAL_HYPE_URL,
        {
            "chainId": chain_id,
            "sentiment": sentiment,
            "socialLanguage": "ALL",
            "targetLanguage": "en",
            "timeRange": time_range,
        },
    )

    items = (data.get("data", {}) or {}).get("leaderBoardList", []) or []
    result = []
    for item in items:
        meta = item.get("metaInfo", {}) or {}
        market = item.get("marketInfo", {}) or {}
        social = item.get("socialHypeInfo", {}) or {}
        result.append(
            {
                "symbol": meta.get("symbol", ""),
                "chain_id": meta.get("chainId", ""),
                "contract_address": meta.get("contractAddress", ""),
                "market_cap": market.get("marketCap", 0),
                "price_change_pct": market.get("priceChange", 0),
                "social_hype_score": social.get("socialHype", 0),
                "sentiment": social.get("sentiment", "Neutral"),
                "summary": social.get("socialSummaryBriefTranslated")
                or social.get("socialSummaryBrief")
                or "",
                "detail": social.get("socialSummaryDetailTranslated")
                or social.get("socialSummaryDetail")
                or "",
                "logo_url": _icon_url(meta.get("logo", "")),
            }
        )

    _cache_set(cache_key, result)
    return result


async def get_trending_tokens(
    chain: str = "solana",
    rank_type: int = 10,
    period: int = 50,
    size: int = 20,
) -> list[dict[str, Any]]:
    """
    Get token rankings.
    rank_type: 10=Trending, 11=TopSearch, 20=Alpha, 40=Stock
    period: 10=1m, 20=5m, 30=1h, 40=4h, 50=24h
    """
    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "CT_501")
    cache_key = f"trending:{chain_id}:{rank_type}:{period}:{size}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _post(
        UNIFIED_RANK_URL,
        {
            "rankType": rank_type,
            "chainId": chain_id,
            "period": period,
            "sortBy": 70,  # Sort by volume
            "orderAsc": False,
            "page": 1,
            "size": min(size, 200),
        },
    )

    tokens = (data.get("data", {}) or {}).get("tokens", []) or []
    result = []
    for t in tokens:
        result.append(
            {
                "symbol": t.get("symbol", ""),
                "chain_id": t.get("chainId", ""),
                "contract_address": t.get("contractAddress", ""),
                "price": t.get("price", "0"),
                "market_cap": t.get("marketCap", "0"),
                "volume_24h": t.get("volume24h", t.get("volume", "0")),
                "liquidity": t.get("liquidity", "0"),
                "holders": t.get("holders", "0"),
                "price_change_24h": t.get("percentChange24h", "0"),
                "price_change_1h": t.get("percentChange1h", "0"),
                "logo_url": _icon_url(t.get("icon", "")),
            }
        )

    _cache_set(cache_key, result)
    return result


async def get_smart_money_inflow(
    chain: str = "solana",
    period: str = "24h",
) -> list[dict[str, Any]]:
    """
    Get tokens ranked by smart money net inflow (USD).
    Shows which tokens professional wallets are accumulating.
    """
    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "CT_501")
    cache_key = f"smart_inflow:{chain_id}:{period}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _post(
        SMART_MONEY_INFLOW_URL,
        {
            "chainId": chain_id,
            "period": period,
            "tagType": 2,
        },
    )

    items = data.get("data", []) or []
    result = []
    for t in items:
        result.append(
            {
                "symbol": t.get("tokenName", ""),
                "contract_address": t.get("ca", ""),
                "price": t.get("price", "0"),
                "market_cap": t.get("marketCap", "0"),
                "volume": t.get("volume", "0"),
                "liquidity": t.get("liquidity", "0"),
                "price_change_pct": t.get("priceChangeRate", "0"),
                "inflow_usd": float(t.get("inflow") or 0),
                "smart_traders": t.get("traders", 0),
                "holders": t.get("holders", "0"),
                "risk_level": t.get("tokenRiskLevel", -1),
                "logo_url": _icon_url(t.get("tokenIconUrl", "")),
            }
        )

    _cache_set(cache_key, result)
    return result


async def get_pnl_leaderboard(
    chain: str = "solana",
    period: str = "30d",
    page_size: int = 25,
) -> list[dict[str, Any]]:
    """
    Get top trader addresses ranked by PnL.
    Useful to cross-reference with Polymarket trader wallets.
    """
    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "CT_501")
    cache_key = f"pnl_board:{chain_id}:{period}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _get(
        PNL_LEADERBOARD_URL,
        {
            "chainId": chain_id,
            "period": period,
            "tag": "ALL",
            "sortBy": 0,
            "orderBy": 0,
            "pageNo": 1,
            "pageSize": min(page_size, 25),
        },
    )

    items = (data.get("data", {}) or {}).get("data", []) or []
    result = []
    for addr in items:
        result.append(
            {
                "address": addr.get("address", ""),
                "label": addr.get("addressLabel", ""),
                "realized_pnl": addr.get("realizedPnl", "0"),
                "realized_pnl_pct": addr.get("realizedPnlPercent", "0"),
                "win_rate": addr.get("winRate", "0"),
                "total_volume": addr.get("totalVolume", "0"),
                "total_tx_count": addr.get("totalTxCnt", 0),
                "tokens_traded": addr.get("totalTradedTokens", 0),
                "top_earning_tokens": [
                    {
                        "symbol": tok.get("tokenSymbol", ""),
                        "pnl": tok.get("realizedPnl", "0"),
                    }
                    for tok in (addr.get("topEarningTokens", []) or [])[:3]
                ],
                "tags": [
                    t.get("tagName", "") for t in (addr.get("genericAddressTagList", []) or [])
                ],
            }
        )

    _cache_set(cache_key, result)
    return result


# ═══════════════════════════════════════════════════════════
# 3. TOKEN INFO — Search, market data, K-line charts
# ═══════════════════════════════════════════════════════════

TOKEN_SEARCH_URL = (
    f"{BINANCE_WEB3_BASE}/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search"
)
TOKEN_DYNAMIC_URL = (
    f"{BINANCE_WEB3_BASE}/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info"
)
TOKEN_KLINE_URL = f"{KLINE_BASE}/u-kline/v1/k-line/candles"

CHAIN_TO_PLATFORM = {
    "56": "bsc",
    "1": "eth",
    "CT_501": "solana",
    "8453": "base",
}


async def search_token(
    keyword: str,
    chains: str = "56,8453,CT_501",
) -> list[dict[str, Any]]:
    """
    Search for tokens by name, symbol, or contract address.
    Returns matching tokens with price, volume, market cap.
    """
    if not keyword.strip():
        return []

    cache_key = f"token_search:{keyword.lower()}:{chains}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _get(
        TOKEN_SEARCH_URL,
        {
            "keyword": keyword.strip(),
            "chainIds": chains,
            "orderBy": "volume24h",
        },
    )

    tokens = data.get("data", []) or []
    result = []
    for t in tokens[:10]:
        result.append(
            {
                "name": t.get("name", ""),
                "symbol": t.get("symbol", ""),
                "chain_id": t.get("chainId", ""),
                "contract_address": t.get("contractAddress", ""),
                "price": t.get("price", "0"),
                "price_change_24h": t.get("percentChange24h", "0"),
                "volume_24h": t.get("volume24h", "0"),
                "market_cap": t.get("marketCap", "0"),
                "liquidity": t.get("liquidity", "0"),
                "logo_url": _icon_url(t.get("icon", "")),
            }
        )

    _cache_set(cache_key, result)
    return result


async def get_token_dynamic_data(
    chain_id: str,
    contract_address: str,
) -> dict[str, Any]:
    """
    Get real-time token market data: price, volume, holders, liquidity, etc.
    """
    if not contract_address.strip():
        return {}

    cache_key = f"token_dynamic:{chain_id}:{contract_address}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _get(
        TOKEN_DYNAMIC_URL,
        {
            "chainId": chain_id,
            "contractAddress": contract_address,
        },
    )

    d = data.get("data", {}) or {}
    if not d:
        return {}

    result = {
        "price": d.get("price", "0"),
        "price_change_5m": d.get("percentChange5m", "0"),
        "price_change_1h": d.get("percentChange1h", "0"),
        "price_change_4h": d.get("percentChange4h", "0"),
        "price_change_24h": d.get("percentChange24h", "0"),
        "price_high_24h": d.get("priceHigh24h", "0"),
        "price_low_24h": d.get("priceLow24h", "0"),
        "volume_24h": d.get("volume24h", "0"),
        "volume_24h_buy": d.get("volume24hBuy", "0"),
        "volume_24h_sell": d.get("volume24hSell", "0"),
        "market_cap": d.get("marketCap", "0"),
        "fdv": d.get("fdv", "0"),
        "liquidity": d.get("liquidity", "0"),
        "holders": d.get("holders", "0"),
        "smart_money_holders": d.get("smartMoneyHolders", "0"),
        "smart_money_holding_pct": d.get("smartMoneyHoldingPercent", "0"),
        "kol_holders": d.get("kolHolders", "0"),
        "top10_holders_pct": d.get("top10HoldersPercentage", "0"),
    }

    _cache_set(cache_key, result)
    return result


async def get_token_kline(
    contract_address: str,
    platform: str = "solana",
    interval: str = "1h",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Get K-line (candlestick) data for technical analysis.
    Returns OHLCV candles.
    """
    if not contract_address.strip():
        return []

    cache_key = f"kline:{platform}:{contract_address}:{interval}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _get(
        TOKEN_KLINE_URL,
        {
            "address": contract_address,
            "platform": platform,
            "interval": interval,
            "limit": min(limit, 500),
        },
    )

    candles_raw = data.get("data", []) or []
    result = []
    for c in candles_raw:
        if isinstance(c, list) and len(c) >= 7:
            result.append(
                {
                    "open": c[0],
                    "high": c[1],
                    "low": c[2],
                    "close": c[3],
                    "volume": c[4],
                    "timestamp": c[5],
                    "tx_count": c[6],
                }
            )

    _cache_set(cache_key, result)
    return result


# ═══════════════════════════════════════════════════════════
# 4. HIGH-LEVEL AGGREGATORS — for bot consumption
# ═══════════════════════════════════════════════════════════


async def get_crypto_market_context(
    query_tokens: list[str] | None = None,
    chain: str = "solana",
) -> dict[str, Any]:
    """
    Aggregate multiple Binance data sources into a single context dict
    suitable for feeding into LLM prompts.

    Args:
        query_tokens: Optional token symbols/names to search for (e.g. ["BTC", "ETH"])
        chain: Default chain to query

    Returns:
        Structured dict with: smart_signals, social_hype, smart_money_inflow,
        trending_tokens, token_data (if query_tokens provided)
    """
    context: dict[str, Any] = {}
    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "CT_501")

    # The four ranking sources are independent — fetch them concurrently so
    # a slow endpoint cannot multiply the total latency.
    async def fetch_signals() -> tuple[list[dict[str, Any]], str]:
        try:
            signals = await get_active_buy_signals(chain=chain)
            top = signals[:10]
            return top, _summarize_signals(top)
        except Exception as exc:
            logger.warning(f"Failed to fetch smart money signals: {exc}")
            return [], "Smart money signals unavailable."

    async def fetch_hype() -> tuple[list[dict[str, Any]], str]:
        try:
            hype = await get_social_hype_ranking(chain=chain)
            top = hype[:10]
            return top, _summarize_social_hype(top)
        except Exception as exc:
            logger.warning(f"Failed to fetch social hype: {exc}")
            return [], "Social hype data unavailable."

    async def fetch_inflow() -> tuple[list[dict[str, Any]], str]:
        try:
            inflow = await get_smart_money_inflow(chain=chain)
            top = inflow[:10]
            return top, _summarize_inflow(top)
        except Exception as exc:
            logger.warning(f"Failed to fetch smart money inflow: {exc}")
            return [], "Smart money inflow data unavailable."

    async def fetch_trending() -> list[dict[str, Any]]:
        try:
            trending = await get_trending_tokens(chain=chain)
            return trending[:10]
        except Exception as exc:
            logger.warning(f"Failed to fetch trending tokens: {exc}")
            return []

    (
        (signals, signals_summary),
        (hype, hype_summary),
        (inflow, inflow_summary),
        trending,
    ) = await asyncio.gather(
        fetch_signals(),
        fetch_hype(),
        fetch_inflow(),
        fetch_trending(),
    )

    context["smart_money_signals"] = signals
    context["smart_money_signals_summary"] = signals_summary
    context["social_hype"] = hype
    context["social_hype_summary"] = hype_summary
    context["smart_money_inflow"] = inflow
    context["smart_money_inflow_summary"] = inflow_summary
    context["trending_tokens"] = trending

    # Specific token lookups — scoped to the requested chain so e.g.
    # Ethereum tokens are not excluded by the default chain filter.
    if query_tokens:
        token_data = {}
        for symbol in query_tokens[:5]:
            try:
                results = await search_token(symbol, chains=chain_id)
                if results:
                    best = results[0]
                    dynamic = await get_token_dynamic_data(
                        best["chain_id"], best["contract_address"]
                    )
                    token_data[symbol] = {**best, "dynamic": dynamic}
            except Exception as exc:
                logger.warning(f"Failed to fetch token data for {symbol}: {exc}")
        context["token_data"] = token_data

    return context


def format_binance_context_for_prompt(context: dict[str, Any]) -> str:
    """
    Format aggregated Binance context into a human-readable string
    suitable for inclusion in LLM prompts.
    """
    parts = []

    signals_summary = context.get("smart_money_signals_summary", "")
    if signals_summary:
        parts.append(f"== Binance Smart Money Signals ==\n{signals_summary}")

    hype_summary = context.get("social_hype_summary", "")
    if hype_summary:
        parts.append(f"== Binance Social Hype Sentiment ==\n{hype_summary}")

    inflow_summary = context.get("smart_money_inflow_summary", "")
    if inflow_summary:
        parts.append(f"== Binance Smart Money Inflow ==\n{inflow_summary}")

    # Specific token data
    token_data = context.get("token_data", {})
    if token_data:
        token_lines = ["== Binance Token Data =="]
        for symbol, data in token_data.items():
            dyn = data.get("dynamic", {})
            token_lines.append(
                f"• {symbol}: ${data.get('price', '?')} "
                f"(24h: {dyn.get('price_change_24h', '?')}%) "
                f"Vol: ${dyn.get('volume_24h', '?')} "
                f"MCap: ${dyn.get('market_cap', '?')} "
                f"SmartMoney Holders: {dyn.get('smart_money_holders', '?')}"
            )
        parts.append("\n".join(token_lines))

    return "\n\n".join(parts) if parts else "No Binance market data available."


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════


def _icon_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return f"https://bin.bnbstatic.com{path}"


def _flatten_tags(tag_obj: dict) -> list[str]:
    tags = []
    if not isinstance(tag_obj, dict):
        return tags
    for _category, tag_list in tag_obj.items():
        if isinstance(tag_list, list):
            for t in tag_list:
                name = t.get("tagName", "") if isinstance(t, dict) else str(t)
                if name:
                    tags.append(name)
    return tags


def _summarize_signals(signals: list[dict]) -> str:
    if not signals:
        return "No active smart money signals."
    lines = []
    for s in signals[:8]:
        gain = s.get("max_gain_pct", "0")
        lines.append(
            f"• {s['ticker']} — {s['direction'].upper()} signal, "
            f"{s['smart_money_count']} smart wallets, "
            f"alert: ${s['alert_price']}, current: ${s['current_price']}, "
            f"max gain: {gain}%, exit rate: {s['exit_rate']}%"
        )
    return "\n".join(lines)


def _summarize_social_hype(items: list[dict]) -> str:
    if not items:
        return "No social hype data."
    lines = []
    for item in items[:8]:
        lines.append(
            f"• {item['symbol']} — hype score: {item['social_hype_score']}, "
            f"sentiment: {item['sentiment']}, "
            f"price Δ: {item['price_change_pct']}%, "
            f"summary: {item['summary'][:100]}"
        )
    return "\n".join(lines)


def _summarize_inflow(items: list[dict]) -> str:
    if not items:
        return "No smart money inflow data."
    lines = []
    for item in items[:8]:
        lines.append(
            f"• {item['symbol']} — net inflow: ${item['inflow_usd']:,.0f}, "
            f"{item['smart_traders']} smart traders, "
            f"price Δ: {item['price_change_pct']}%, "
            f"risk: {_risk_label(item['risk_level'])}"
        )
    return "\n".join(lines)


def _risk_label(level: int) -> str:
    return {1: "low", 2: "medium", 3: "high"}.get(level, "unknown")
