"""Binance Skills Hub API routes — exposes smart money signals,
social hype rankings, and token market data to the frontend dashboard."""

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.routes.auth import get_current_user_from_token
from app.config import get_settings
from app.utils.time import utc_now

router = APIRouter(prefix="/api/binance", tags=["binance-signals"])
settings = get_settings()
logger = logging.getLogger(__name__)

BASE_URL = "https://web3.binance.com"

# ── In-memory TTL cache ─────────────────────────────────────────────
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


# ── Supported chains ────────────────────────────────────────────────
SUPPORTED_CHAINS = {
    "ethereum": "1",
    "polygon": "137",
    "bsc": "56",
    "arbitrum": "42161",
    "base": "8453",
    "solana": "501",
}


# ── Response schemas ────────────────────────────────────────────────


class SmartMoneySignal(BaseModel):
    token_address: str = ""
    token_symbol: str = ""
    token_name: str = ""
    chain: str = ""
    signal_type: str = ""
    buy_count: int = 0
    sell_count: int = 0
    net_flow_usd: float = 0.0
    smart_money_holders: int = 0
    icon_url: str = ""


class SocialHypeToken(BaseModel):
    token_symbol: str = ""
    token_name: str = ""
    rank: int = 0
    mentions: int = 0
    sentiment_score: float = 0.0
    price_change_24h: float = 0.0
    icon_url: str = ""


class TrendingToken(BaseModel):
    token_symbol: str = ""
    token_name: str = ""
    rank: int = 0
    chain: str = ""
    price_usd: float = 0.0
    price_change_24h: float = 0.0
    volume_24h: float = 0.0
    market_cap: float = 0.0
    icon_url: str = ""


class BinanceDashboardResponse(BaseModel):
    smart_money_signals: list[dict[str, Any]] = []
    social_hype: list[dict[str, Any]] = []
    trending_tokens: list[dict[str, Any]] = []
    smart_money_inflow: list[dict[str, Any]] = []
    pnl_leaderboard: list[dict[str, Any]] = []
    fetched_at: str = ""
    enabled: bool = True


# ── Helper: async fetch from Binance ────────────────────────────────


async def _binance_post(path: str, payload: dict, cache_key: str | None = None) -> Any:
    """POST to Binance web3 API with caching."""
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    url = f"{BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "PolymarketBot/1.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("data", data)
            if cache_key:
                _cache_set(cache_key, result)
            return result
    except Exception as e:
        logger.warning(f"Binance API call failed ({path}): {e}")
        return None


async def _binance_get(path: str, params: dict | None = None, cache_key: str | None = None) -> Any:
    """GET from Binance web3 API with caching."""
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    url = f"{BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                params=params,
                headers={
                    "User-Agent": "PolymarketBot/1.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("data", data)
            if cache_key:
                _cache_set(cache_key, result)
            return result
    except Exception as e:
        logger.warning(f"Binance GET failed ({path}): {e}")
        return None


# ── Routes ──────────────────────────────────────────────────────────


@router.get("/signals/smart-money", response_model=list[dict[str, Any]])
async def get_smart_money_signals(
    chain: str = Query("ethereum", description="Chain name"),
    limit: int = Query(20, ge=1, le=50),
    _user: dict = Depends(get_current_user_from_token),
):
    """Get current smart money buy/sell signals from Binance."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "1")
    payload = {
        "chainId": chain_id,
        "type": "ALL",
        "pageIndex": 1,
        "pageSize": limit,
    }
    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money",
        payload,
        cache_key=f"smart_signals_{chain_id}_{limit}",
    )
    if data is None:
        return []

    signals = data if isinstance(data, list) else data.get("rows", data.get("signals", []))
    return signals[:limit]


@router.get("/signals/active-buys", response_model=list[dict[str, Any]])
async def get_active_buy_signals(
    chain: str = Query("ethereum", description="Chain name"),
    limit: int = Query(10, ge=1, le=30),
    _user: dict = Depends(get_current_user_from_token),
):
    """Get tokens currently being accumulated by smart money wallets."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "1")
    payload = {
        "chainId": chain_id,
        "type": "BUY",
        "pageIndex": 1,
        "pageSize": limit,
    }
    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money",
        payload,
        cache_key=f"active_buys_{chain_id}_{limit}",
    )
    if data is None:
        return []

    signals = data if isinstance(data, list) else data.get("rows", data.get("signals", []))
    return signals[:limit]


@router.get("/rankings/social-hype", response_model=list[dict[str, Any]])
async def get_social_hype_ranking(
    limit: int = Query(20, ge=1, le=50),
    _user: dict = Depends(get_current_user_from_token),
):
    """Get tokens ranked by social media hype / mentions."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/social-hype",
        {"pageIndex": 1, "pageSize": limit},
        cache_key=f"social_hype_{limit}",
    )
    if data is None:
        return []

    rows = data if isinstance(data, list) else data.get("rows", data.get("rankings", []))
    return rows[:limit]


@router.get("/rankings/trending", response_model=list[dict[str, Any]])
async def get_trending_tokens(
    limit: int = Query(20, ge=1, le=50),
    _user: dict = Depends(get_current_user_from_token),
):
    """Get unified trending token rankings."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/unified-token-rank",
        {"pageIndex": 1, "pageSize": limit},
        cache_key=f"trending_{limit}",
    )
    if data is None:
        return []

    rows = data if isinstance(data, list) else data.get("rows", data.get("rankings", []))
    return rows[:limit]


@router.get("/rankings/smart-money-inflow", response_model=list[dict[str, Any]])
async def get_smart_money_inflow(
    limit: int = Query(20, ge=1, le=50),
    _user: dict = Depends(get_current_user_from_token),
):
    """Get tokens with highest smart money net inflow."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/smart-money-inflow",
        {"pageIndex": 1, "pageSize": limit},
        cache_key=f"smart_inflow_{limit}",
    )
    if data is None:
        return []

    rows = data if isinstance(data, list) else data.get("rows", data.get("rankings", []))
    return rows[:limit]


@router.get("/rankings/pnl-leaderboard", response_model=list[dict[str, Any]])
async def get_pnl_leaderboard(
    period: str = Query("7d", description="7d or 30d"),
    limit: int = Query(20, ge=1, le=50),
    _user: dict = Depends(get_current_user_from_token),
):
    """Get top PnL traders leaderboard."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/pnl-leaderboard",
        {"periodType": period, "pageIndex": 1, "pageSize": limit},
        cache_key=f"pnl_leaders_{period}_{limit}",
    )
    if data is None:
        return []

    rows = data if isinstance(data, list) else data.get("rows", data.get("rankings", []))
    return rows[:limit]


@router.get("/token/search", response_model=list[dict[str, Any]])
async def search_token(
    query: str = Query(..., description="Token name or symbol"),
    _user: dict = Depends(get_current_user_from_token),
):
    """Search for a token by name or symbol across chains."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/token/search",
        {"keyword": query, "pageSize": 10},
        cache_key=f"token_search_{query.lower()}",
    )
    if data is None:
        return []

    rows = data if isinstance(data, list) else data.get("rows", data.get("tokens", []))
    return rows[:10]


@router.get("/token/data", response_model=dict[str, Any])
async def get_token_data(
    address: str = Query(..., description="Token contract address"),
    chain: str = Query("ethereum", description="Chain name"),
    _user: dict = Depends(get_current_user_from_token),
):
    """Get detailed dynamic data for a specific token."""
    if not settings.binance_skills_enabled:
        raise HTTPException(status_code=503, detail="Binance Skills integration is disabled")

    chain_id = SUPPORTED_CHAINS.get(chain.lower(), "1")
    data = await _binance_post(
        "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/token/dynamic-data",
        {"chainId": chain_id, "address": address},
        cache_key=f"token_data_{chain_id}_{address}",
    )
    return data or {}


@router.get("/dashboard", response_model=BinanceDashboardResponse)
async def get_binance_dashboard(
    _user: dict = Depends(get_current_user_from_token),
):
    """Aggregated Binance signals dashboard — returns smart money signals,
    social hype, trending tokens, and inflow data in a single call."""
    if not settings.binance_skills_enabled:
        return BinanceDashboardResponse(enabled=False, fetched_at=utc_now().isoformat())

    import asyncio

    # Fetch all data sources in parallel
    results = await asyncio.gather(
        _binance_post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money",
            {"chainId": "1", "type": "BUY", "pageIndex": 1, "pageSize": 10},
            cache_key="dash_smart_signals",
        ),
        _binance_post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/social-hype",
            {"pageIndex": 1, "pageSize": 10},
            cache_key="dash_social_hype",
        ),
        _binance_post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/unified-token-rank",
            {"pageIndex": 1, "pageSize": 10},
            cache_key="dash_trending",
        ),
        _binance_post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/smart-money-inflow",
            {"pageIndex": 1, "pageSize": 10},
            cache_key="dash_inflow",
        ),
        _binance_post(
            "/bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/pnl-leaderboard",
            {"periodType": "7d", "pageIndex": 1, "pageSize": 10},
            cache_key="dash_pnl",
        ),
        return_exceptions=True,
    )

    def _extract(data: Any) -> list:
        if data is None or isinstance(data, Exception):
            return []
        if isinstance(data, list):
            return data[:10]
        if isinstance(data, dict):
            return (data.get("rows") or data.get("signals") or data.get("rankings") or [])[:10]
        return []

    return BinanceDashboardResponse(
        smart_money_signals=_extract(results[0]),
        social_hype=_extract(results[1]),
        trending_tokens=_extract(results[2]),
        smart_money_inflow=_extract(results[3]),
        pnl_leaderboard=_extract(results[4]),
        fetched_at=utc_now().isoformat(),
        enabled=True,
    )
