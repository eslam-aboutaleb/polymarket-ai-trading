"""Open-source research MCP server (stdio JSON protocol).

Tools:
- web_search(query, recency_hours=24, max_results=8)        — DuckDuckGo free search
- x_posts_search(query, recency_hours=24, max_results=8)    — X/Twitter via DDG
- tavily_search(query, max_results=5, search_depth="basic")  — Tavily premium search (opt-in)
- news_search(query, max_results=5, sort_by="relevancy")     — NewsAPI headlines (opt-in)
- binance_smart_signals(chain, page_size)                     — Smart money buy/sell signals
- binance_social_hype(chain)                                  — Social hype sentiment rankings
- binance_smart_money_inflow(chain, period)                   — Smart money net inflow rankings
- binance_trending_tokens(chain, rank_type)                   — Trending/top-search token rankings
- binance_token_search(keyword)                               — Search tokens by name/symbol
- binance_token_data(chain_id, contract_address)              — Real-time token market data
- binance_market_context(query_tokens, chain)                 — Aggregated Binance context for LLM
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from ddgs import DDGS

# ── Binance Skills Hub integration ────────────────────────
try:
    from binance_skills import (
        get_smart_money_signals,
        get_active_buy_signals,
        get_social_hype_ranking,
        get_smart_money_inflow,
        get_trending_tokens,
        get_pnl_leaderboard,
        search_token,
        get_token_dynamic_data,
        get_crypto_market_context,
    )
    # Module-level import for tool dispatch
    import binance_skills
    _BINANCE_AVAILABLE = True
except ImportError:
    _BINANCE_AVAILABLE = False
    binance_skills = None  # type: ignore


def _run_async(coro):
    """Run an async coroutine synchronously (for stdio server)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result(timeout=30)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)

# ── Optional premium search providers ──────────────────────
_tavily_client = None
_newsapi_client = None


def _get_tavily():
    """Lazy-init Tavily client (needs TAVILY_API_KEY env var)."""
    global _tavily_client
    if _tavily_client is not None:
        return _tavily_client
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from tavily import TavilyClient
        _tavily_client = TavilyClient(api_key=api_key)
        return _tavily_client
    except ImportError:
        return None


def _get_newsapi():
    """Lazy-init NewsAPI client (needs NEWSAPI_API_KEY env var)."""
    global _newsapi_client
    if _newsapi_client is not None:
        return _newsapi_client
    api_key = os.environ.get("NEWSAPI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from newsapi import NewsApiClient
        _newsapi_client = NewsApiClient(api_key=api_key)
        return _newsapi_client
    except ImportError:
        return None


def _timelimit_for_hours(recency_hours: int) -> str | None:
    if recency_hours <= 24:
        return "d"
    if recency_hours <= 24 * 7:
        return "w"
    if recency_hours <= 24 * 31:
        return "m"
    return "y"


def _normalize_result(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("title") or "").strip(),
        "url": str(item.get("href") or "").strip(),
        "snippet": str(item.get("body") or "").strip(),
    }


def web_search(
    query: str,
    recency_hours: int = 24,
    max_results: int = 8,
) -> list[dict[str, str]]:
    if not query.strip():
        return []

    timelimit = _timelimit_for_hours(max(1, recency_hours))
    results: list[dict[str, str]] = []
    with DDGS() as ddgs:
        for row in ddgs.text(
            keywords=query,
            max_results=max(1, min(20, int(max_results))),
            timelimit=timelimit,
        ):
            normalized = _normalize_result(row or {})
            if normalized["url"]:
                results.append(normalized)
    return results[: max(1, min(20, int(max_results)))]


def x_posts_search(
    query: str,
    recency_hours: int = 24,
    max_results: int = 8,
) -> list[dict[str, str]]:
    if not query.strip():
        return []

    primary_query = f"site:x.com {query}"
    primary = web_search(
        primary_query,
        recency_hours=recency_hours,
        max_results=max_results,
    )
    x_only = [
        r
        for r in primary
        if "x.com/" in r.get("url", "") or "twitter.com/" in r.get("url", "")
    ]
    if x_only:
        return x_only[: max(1, min(20, int(max_results)))]

    fallback_query = f"{query} x.com"
    fallback = web_search(
        fallback_query,
        recency_hours=recency_hours,
        max_results=max_results,
    )
    return [
        r
        for r in fallback
        if "x.com/" in r.get("url", "") or "twitter.com/" in r.get("url", "")
    ][: max(1, min(20, int(max_results)))]


def _run_one(payload: dict[str, Any]) -> dict[str, Any]:
    tool = str(payload.get("tool") or "").strip()
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    if tool == "web_search":
        result = web_search(
            query=str(args.get("query") or ""),
            recency_hours=int(args.get("recency_hours", 24) or 24),
            max_results=int(args.get("max_results", 8) or 8),
        )
    elif tool == "x_posts_search":
        result = x_posts_search(
            query=str(args.get("query") or ""),
            recency_hours=int(args.get("recency_hours", 24) or 24),
            max_results=int(args.get("max_results", 8) or 8),
        )
    elif tool == "tavily_search":
        result = tavily_search(
            query=str(args.get("query") or ""),
            max_results=int(args.get("max_results", 5) or 5),
            search_depth=str(args.get("search_depth", "basic") or "basic"),
        )
    elif tool == "news_search":
        result = news_search(
            query=str(args.get("query") or ""),
            max_results=int(args.get("max_results", 5) or 5),
            sort_by=str(args.get("sort_by", "relevancy") or "relevancy"),
        )
    # ── Binance Skills tools ──────────────────────────────
    elif tool == "binance_smart_signals":
        result = _run_async(binance_skills.get_smart_money_signals(
            chain=str(args.get("chain", "solana")),
            page_size=int(args.get("page_size", 50) or 50),
        ))
    elif tool == "binance_active_buy_signals":
        result = _run_async(binance_skills.get_active_buy_signals(
            chain=str(args.get("chain", "solana")),
            min_smart_money=int(args.get("min_smart_money", 2) or 2),
        ))
    elif tool == "binance_social_hype":
        result = _run_async(binance_skills.get_social_hype_ranking(
            chain=str(args.get("chain", "solana")),
        ))
    elif tool == "binance_smart_money_inflow":
        result = _run_async(binance_skills.get_smart_money_inflow(
            chain=str(args.get("chain", "solana")),
            period=str(args.get("period", "24h")),
        ))
    elif tool == "binance_trending_tokens":
        result = _run_async(binance_skills.get_trending_tokens(
            chain=str(args.get("chain", "solana")),
            rank_type=int(args.get("rank_type", 10) or 10),
        ))
    elif tool == "binance_token_search":
        result = _run_async(binance_skills.search_token(
            keyword=str(args.get("keyword", "")),
        ))
    elif tool == "binance_token_data":
        result = _run_async(binance_skills.get_token_dynamic_data(
            chain_id=str(args.get("chain_id", "")),
            contract_address=str(args.get("contract_address", "")),
        ))
    elif tool == "binance_market_context":
        query_tokens = args.get("query_tokens", [])
        if isinstance(query_tokens, str):
            query_tokens = [t.strip() for t in query_tokens.split(",") if t.strip()]
        result = _run_async(binance_skills.get_crypto_market_context(
            query_tokens=query_tokens or None,
            chain=str(args.get("chain", "solana")),
        ))
    elif tool == "binance_pnl_leaderboard":
        result = _run_async(binance_skills.get_pnl_leaderboard(
            chain=str(args.get("chain", "solana")),
            period=str(args.get("period", "30d")),
        ))
    else:
        return {"ok": False, "error": f"Unsupported tool: {tool}"}
    return {"ok": True, "result": result}


# ── Tavily premium search ──────────────────────────────────

def tavily_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
) -> list[dict[str, str]]:
    """
    Search using Tavily — premium AI-optimised search engine.
    Returns results with title, url, snippet (content).
    Falls back to DuckDuckGo if Tavily is unavailable.
    """
    if not query.strip():
        return []

    client = _get_tavily()
    if client is None:
        # Graceful fallback to DuckDuckGo
        return web_search(query, max_results=max_results)

    try:
        response = client.search(
            query=query,
            max_results=max(1, min(10, max_results)),
            search_depth=search_depth,  # "basic" (fast) or "advanced" (deeper)
        )
        results = []
        for item in response.get("results", []):
            results.append({
                "title": str(item.get("title", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "snippet": str(item.get("content", "")).strip(),
                "score": str(item.get("score", "")),
            })
        return results[:max(1, min(10, max_results))]
    except Exception as exc:
        # Fallback to DuckDuckGo on failure
        return web_search(query, max_results=max_results)


# ── NewsAPI integration ────────────────────────────────────

def news_search(
    query: str,
    max_results: int = 5,
    sort_by: str = "relevancy",
) -> list[dict[str, str]]:
    """
    Search recent news articles via NewsAPI.
    Returns results with title, url, snippet (description), source, publishedAt.
    Falls back to DuckDuckGo news if NewsAPI is unavailable.
    """
    if not query.strip():
        return []

    client = _get_newsapi()
    if client is None:
        # Fallback: use DuckDuckGo news search
        return web_search(f"{query} news", max_results=max_results)

    try:
        response = client.get_everything(
            q=query,
            language="en",
            sort_by=sort_by,  # relevancy, popularity, publishedAt
            page_size=max(1, min(20, max_results)),
        )
        results = []
        for article in response.get("articles", []):
            results.append({
                "title": str(article.get("title", "")).strip(),
                "url": str(article.get("url", "")).strip(),
                "snippet": str(article.get("description", "")).strip(),
                "source": str((article.get("source") or {}).get("name", "")).strip(),
                "published_at": str(article.get("publishedAt", "")).strip(),
            })
        return results[:max(1, min(20, max_results))]
    except Exception:
        return web_search(f"{query} news", max_results=max_results)


def main() -> int:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Request must be an object")
            response = _run_one(payload)
        except Exception as exc:  # pragma: no cover
            response = {"ok": False, "error": str(exc)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
