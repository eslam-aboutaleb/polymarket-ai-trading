"""Open-source research MCP server (stdio JSON protocol).

Protocol
--------
One JSON object per stdin line: ``{"tool": <name>, "args": {...}}``. Exactly one
JSON object is written back per request line, in request order, so a client may
pipeline several requests in one process. The special ``__shutdown__`` tool
acknowledges and then stops the read loop (a client that simply closes stdin
gets the same outcome via EOF).

Tools (the dispatch table is :data:`TOOLS`, built at import):
- web_search(query, recency_hours=24, max_results=8)        — DuckDuckGo free search
- x_indexed_web_search(query, recency_hours=24, max_results=8) — indexed X links via DDG
- x_posts_search(query, recency_hours=24, max_results=8)    — compatibility alias for
  ``x_indexed_web_search`` (the same implementation, so existing clients keep working)
- tavily_search(query, max_results=5, search_depth="basic")  — Tavily premium search (opt-in)
- news_search(query, max_results=5, sort_by="relevancy")     — NewsAPI headlines (opt-in)
- binance_smart_signals(chain, page_size)                     — Smart money buy/sell signals
- binance_active_buy_signals(chain, min_smart_money)          — Active smart-money buy signals
- binance_social_hype(chain)                                  — Social hype sentiment rankings
- binance_smart_money_inflow(chain, period)                   — Smart money net inflow rankings
- binance_trending_tokens(chain, rank_type)                   — Trending/top-search token rankings
- binance_token_search(keyword)                               — Search tokens by name/symbol
- binance_token_data(chain_id, contract_address)              — Real-time token market data
- binance_market_context(query_tokens, chain)                 — Aggregated Binance context for LLM
- binance_pnl_leaderboard(period, page_size)                  — Smart-money PnL leaderboard
- cross_venue_markets(query, limit)                           — Cached cross-venue rows (backend)

The Binance tools register only when the optional ``binance_skills`` module
imports; when it is missing they answer with the same "module unavailable"
error as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from ddgs import DDGS

# ── Binance Skills Hub integration ────────────────────────
try:
    # Module-level import for tool dispatch. Individual tool functions are
    # reached as `binance_skills.<fn>` rather than imported by name.
    import binance_skills

    _BINANCE_AVAILABLE = True
except ImportError:
    _BINANCE_AVAILABLE = False
    binance_skills = None  # type: ignore


# ── Cross-venue market cache ─────────────────────────────
# The venue cache lives in the backend: this server runs as a stdio
# subprocess of the AI services, which carry no database driver and no
# backend code. The tool therefore reads the same cached rows over HTTP
# and degrades to an empty list — never a raise — exactly like
# ``binance_skills._get`` when the backend is unreachable or the call is
# unauthorized. Both variables are opt-in: unset means no venue signal.
VENUE_API_BASE = os.environ.get("VENUE_API_BASE", "http://backend:8002").strip().rstrip("/")
VENUE_API_TOKEN = os.environ.get("VENUE_API_TOKEN", "").strip()


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


# ── Search provenance labels ─────────────────────────────────
# Every row that reaches a client carries these labels. The "X" source is a
# search-engine index, never an authenticated timeline, and the labels say so
# at each hop instead of only at the tool boundary.
WEB_SOURCE_TYPE = "web_index"
INDEXED_SOURCE_TYPE = "indexed_web"
INDEXED_PROVIDER = "duckduckgo_web_index"
INDEXED_QUALITY = "unverified_search_snippet"
INDEXED_COVERAGE = "partial_non_comprehensive"
# A search snippet carries no reliable publication timestamp: the index lag is
# unknown, so the freshness is stated as unknown rather than guessed.
INDEXED_FRESHNESS = "unknown_index_lag"

# Search status values. "no_matches" and "provider_unavailable" are different
# facts: the first means the index had nothing, the second that the provider
# never answered. Conflating them turns an outage into "no activity".
STATUS_OK = "ok"
STATUS_NO_MATCHES = "no_matches"
STATUS_PROVIDER_UNAVAILABLE = "provider_unavailable"


def _status_for(rows: list[dict[str, str]], error: str | None) -> str:
    """Classify a search outcome: rows, no matches, or a provider failure."""
    if error:
        return STATUS_PROVIDER_UNAVAILABLE
    return STATUS_OK if rows else STATUS_NO_MATCHES


def _rows_of(payload: Any) -> list[dict[str, str]]:
    """Read the result rows from a search payload (dict) or a raw row list."""
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _search_rows(
    query: str,
    recency_hours: int,
    max_results: int,
) -> tuple[list[dict[str, str]], str | None]:
    """Run the DuckDuckGo text search.

    Returns:
        ``(rows, provider_error)``. ``provider_error`` is ``None`` when the
        provider answered, and a human-readable reason (rate limit, timeout,
        blocked request) when it did not, so callers can tell "the index had no
        matches" from "the index was never asked".
    """
    if not query.strip():
        return [], None

    timelimit = _timelimit_for_hours(max(1, recency_hours))
    limit = max(1, min(20, int(max_results)))
    results: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            for row in ddgs.text(
                keywords=query,
                max_results=limit,
                timelimit=timelimit,
            ):
                normalized = _normalize_result(row or {})
                if normalized["url"]:
                    results.append(normalized)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    return results[:limit], None


def web_search(
    query: str,
    recency_hours: int = 24,
    max_results: int = 8,
) -> dict[str, Any]:
    """Search the web index and report the provider status explicitly.

    The payload always carries ``status`` (``ok`` / ``no_matches`` /
    ``provider_unavailable``) next to ``results``: an empty ``results`` list
    alone must never be read as "nothing exists out there".
    """
    rows, error = _search_rows(query, recency_hours, max_results)
    return {
        "status": _status_for(rows, error),
        "source_type": WEB_SOURCE_TYPE,
        "provider": INDEXED_PROVIDER,
        "coverage": INDEXED_COVERAGE,
        "freshness": INDEXED_FRESHNESS,
        "search_query": query,
        "checked_at": datetime.now(UTC).isoformat(),
        "error": error,
        "results": rows,
    }


def _is_indexed_x_post(url: str) -> bool:
    """Accept only genuine X/Twitter status permalinks, never host substrings."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    if hostname not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
    }:
        return False
    parts = parsed.path.strip("/").split("/")
    return (
        len(parts) >= 3 and bool(parts[0]) and parts[1].lower() == "status" and parts[2].isdigit()
    )


def _indexed_x_results(
    items: list[dict[str, str]],
    fetched_at: str,
    query: str = "",
) -> list[dict[str, str]]:
    """Tag indexed snippets as unverified: no native API, author or engagement claims.

    Rows are de-duplicated by (username, status id), so the same status reached
    through x.com and through twitter.com collapses into a single row instead
    of two.
    """
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        url = str(item.get("url") or "")
        if not _is_indexed_x_post(url):
            continue
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        username = parts[0].lower()
        status_id = parts[2]
        if (username, status_id) in seen:
            continue
        seen.add((username, status_id))
        # One canonical permalink per status: the host is normalised to x.com
        # (twitter.com serves the same statuses) and tracking parameters are
        # dropped. The provider's exact URL is kept in "original_url".
        canonical = f"https://x.com{parsed.path.rstrip('/')}"
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": canonical,
                "original_url": url,
                "snippet": str(item.get("snippet") or ""),
                "source_type": INDEXED_SOURCE_TYPE,
                "provider": INDEXED_PROVIDER,
                "quality": INDEXED_QUALITY,
                "coverage": INDEXED_COVERAGE,
                "freshness": INDEXED_FRESHNESS,
                "search_query": query,
                "first_seen_at": fetched_at,
                "fetched_at": fetched_at,
            }
        )
    return results


def _indexed_x_payload(
    status: str,
    query: str,
    rows: list[dict[str, str]],
    error: str | None,
    checked_at: str,
) -> dict[str, Any]:
    """Build the indexed-X tool payload with its provenance labels."""
    return {
        "status": status,
        "source_type": INDEXED_SOURCE_TYPE,
        "provider": INDEXED_PROVIDER,
        "quality": INDEXED_QUALITY,
        "coverage": INDEXED_COVERAGE,
        "freshness": INDEXED_FRESHNESS,
        "search_query": query,
        "checked_at": checked_at,
        "error": error,
        "results": rows,
    }


def x_indexed_web_search(
    query: str,
    recency_hours: int = 24,
    max_results: int = 8,
) -> dict[str, Any]:
    """Search *indexed links to posts*, not an authenticated X timeline.

    A zero-result response means no indexed matches, not that no posts exist:
    ``status`` is ``no_matches`` in that case and ``provider_unavailable``
    when the search provider failed, so an outage is never presented as
    missing social evidence. Never infer native post times, likes, retweets
    or authenticity from snippets.
    """
    checked_at = datetime.now(UTC).isoformat()
    if not query.strip():
        return _indexed_x_payload(STATUS_NO_MATCHES, query, [], None, checked_at)

    limit = max(1, min(20, int(max_results)))
    primary_query = f"site:x.com {query}"
    primary = web_search(primary_query, recency_hours=recency_hours, max_results=limit)
    primary_rows = _indexed_x_results(_rows_of(primary), checked_at, primary_query)
    if primary_rows:
        return _indexed_x_payload(STATUS_OK, primary_query, primary_rows[:limit], None, checked_at)

    fallback_query = f"{query} x.com"
    fallback = web_search(fallback_query, recency_hours=recency_hours, max_results=limit)
    fallback_rows = _indexed_x_results(_rows_of(fallback), checked_at, fallback_query)
    if fallback_rows:
        return _indexed_x_payload(
            STATUS_OK, fallback_query, fallback_rows[:limit], None, checked_at
        )

    # No rows: distinguish "the index had nothing" from "the provider failed".
    errors = [
        str(payload.get("error"))
        for payload in (primary, fallback)
        if isinstance(payload, dict) and payload.get("error")
    ]
    if errors:
        return _indexed_x_payload(
            STATUS_PROVIDER_UNAVAILABLE, fallback_query, [], errors[0], checked_at
        )
    return _indexed_x_payload(STATUS_NO_MATCHES, fallback_query, [], None, checked_at)


def x_posts_search(
    query: str,
    recency_hours: int = 24,
    max_results: int = 8,
) -> list[dict[str, Any]]:
    """Backward-compatible name for :func:`x_indexed_web_search`.

    Existing clients and tests consume a flat list of indexed rows, so this
    alias projects the structured payload back to its ``results`` list. New
    callers should use ``x_indexed_web_search`` to read the ``status`` field
    and tell "no indexed matches" apart from "provider unavailable".
    """
    payload = x_indexed_web_search(
        query=query,
        recency_hours=recency_hours,
        max_results=max_results,
    )
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


async def cross_venue_markets(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Cached cross-venue market groups matching a query (read-only signal).

    Returns the groups served by ``GET /api/venues/markets/cross``: rows
    grouped by normalized question, each carrying the venue prices, both
    venues' questions and the yes-price gap. Signal only — nothing here
    is executable without manual confirmation.

    Degrades to ``[]`` when the venue API base is unset, the backend is
    unreachable, or the call is unauthorized (no ``VENUE_API_TOKEN``
    configured for a route that requires one).
    """
    if not query.strip() or not VENUE_API_BASE:
        return []
    headers = {"Accept": "application/json"}
    if VENUE_API_TOKEN:
        headers["Authorization"] = f"Bearer {VENUE_API_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{VENUE_API_BASE}/api/venues/markets/cross",
                params={"q": query.strip(), "limit": max(1, min(int(limit or 50), 100))},
                headers=headers,
            )
        if resp.status_code != 200:
            return []
        payload = resp.json()
    except Exception:
        return []
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return []
    return [group for group in groups if isinstance(group, dict)]


# ── Tool registry ───────────────────────────────────────────
# Name → callable(args dict), built once at import. The Binance tools are
# added only when the optional module imported above; when it is missing the
# lookup below answers with the same "module unavailable" error as before.


def _tool_web_search(args: dict[str, Any]) -> dict[str, Any]:
    return web_search(
        query=str(args.get("query") or ""),
        recency_hours=int(args.get("recency_hours", 24) or 24),
        max_results=int(args.get("max_results", 8) or 8),
    )


def _tool_x_indexed_web_search(args: dict[str, Any]) -> dict[str, Any]:
    return x_indexed_web_search(
        query=str(args.get("query") or ""),
        recency_hours=int(args.get("recency_hours", 24) or 24),
        max_results=int(args.get("max_results", 8) or 8),
    )


def _tool_tavily_search(args: dict[str, Any]) -> list[dict[str, str]]:
    return tavily_search(
        query=str(args.get("query") or ""),
        max_results=int(args.get("max_results", 5) or 5),
        search_depth=str(args.get("search_depth", "basic") or "basic"),
    )


def _tool_news_search(args: dict[str, Any]) -> list[dict[str, str]]:
    return news_search(
        query=str(args.get("query") or ""),
        max_results=int(args.get("max_results", 5) or 5),
        sort_by=str(args.get("sort_by", "relevancy") or "relevancy"),
    )


def _tool_cross_venue_markets(args: dict[str, Any]) -> list[dict[str, Any]]:
    return _run_async(
        cross_venue_markets(
            query=str(args.get("query") or ""),
            limit=int(args.get("limit", 50) or 50),
        )
    )


def _tool_binance_smart_signals(args: dict[str, Any]):
    return _run_async(
        binance_skills.get_smart_money_signals(
            chain=str(args.get("chain", "solana")),
            page_size=int(args.get("page_size", 50) or 50),
        )
    )


def _tool_binance_active_buy_signals(args: dict[str, Any]):
    return _run_async(
        binance_skills.get_active_buy_signals(
            chain=str(args.get("chain", "solana")),
            min_smart_money=int(args.get("min_smart_money", 2) or 2),
        )
    )


def _tool_binance_social_hype(args: dict[str, Any]):
    return _run_async(
        binance_skills.get_social_hype_ranking(
            chain=str(args.get("chain", "solana")),
        )
    )


def _tool_binance_smart_money_inflow(args: dict[str, Any]):
    return _run_async(
        binance_skills.get_smart_money_inflow(
            chain=str(args.get("chain", "solana")),
            period=str(args.get("period", "24h")),
        )
    )


def _tool_binance_trending_tokens(args: dict[str, Any]):
    return _run_async(
        binance_skills.get_trending_tokens(
            chain=str(args.get("chain", "solana")),
            rank_type=int(args.get("rank_type", 10) or 10),
        )
    )


def _tool_binance_token_search(args: dict[str, Any]):
    return _run_async(
        binance_skills.search_token(
            keyword=str(args.get("keyword", "")),
        )
    )


def _tool_binance_token_data(args: dict[str, Any]):
    return _run_async(
        binance_skills.get_token_dynamic_data(
            chain_id=str(args.get("chain_id", "")),
            contract_address=str(args.get("contract_address", "")),
        )
    )


def _tool_binance_market_context(args: dict[str, Any]):
    query_tokens = args.get("query_tokens", [])
    if isinstance(query_tokens, str):
        query_tokens = [t.strip() for t in query_tokens.split(",") if t.strip()]
    return _run_async(
        binance_skills.get_crypto_market_context(
            query_tokens=query_tokens or None,
            chain=str(args.get("chain", "solana")),
        )
    )


def _tool_binance_pnl_leaderboard(args: dict[str, Any]):
    return _run_async(
        binance_skills.get_pnl_leaderboard(
            period=str(args.get("period", "30d")),
            page_size=int(args.get("page_size", 25) or 25),
        )
    )


async def binary_book_research(args: dict[str, Any]) -> dict[str, Any]:
    """Bounded GET-only bridge; unavailable evidence never becomes an empty success."""
    import re
    from decimal import Decimal, InvalidOperation

    unavailable = {
        "version": "binary-book-v1",
        "status": "abstain",
        "reason": "BOOK_TOOL_UNAVAILABLE",
        "research_only": True,
    }
    condition = str(args.get("condition_id") or "").lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", condition):
        return {**unavailable, "reason": "INVALID_CONDITION_ID"}
    try:
        shares = Decimal(str(args.get("shares", "10")))
        if not shares.is_finite() or not 0 < shares <= 1_000_000_000:
            return {**unavailable, "reason": "INVALID_SIZE"}
        params = {
            "condition_id": condition,
            "shares": str(shares),
            "outcome": str(args.get("outcome", "YES")),
            "side": str(args.get("side", "BUY")),
        }
        for key in ("as_of", "fee_bps", "fee_version"):
            if args.get(key) is not None:
                params[key] = str(args[key])
        headers = {"Accept": "application/json"}
        if VENUE_API_TOKEN:
            headers["Authorization"] = f"Bearer {VENUE_API_TOKEN}"
        async with (
            httpx.AsyncClient(timeout=10) as client,
            client.stream(
                "GET",
                f"{VENUE_API_BASE}/api/microstructure/book",
                params=params,
                headers=headers,
                follow_redirects=False,
            ) as response,
        ):
            if response.status_code != 200:
                return {**unavailable, "reason": "BOOK_BACKEND_DISABLED_OR_UNAVAILABLE"}
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > 524288:
                    return {**unavailable, "reason": "BOOK_RESPONSE_TOO_LARGE"}
                data.extend(chunk)
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("version") != "binary-book-v1":
            return {**unavailable, "reason": "INVALID_BOOK_RESPONSE"}
        # Raw levels and untrusted source prose are not needed in an agent result.
        for book in payload.get("books", {}).values():
            book.pop("bids", None)
            book.pop("asks", None)
        for estimate in payload.get("estimates", []):
            estimate.pop("curve", None)
        return payload
    except (ValueError, TypeError, InvalidOperation, httpx.HTTPError, TimeoutError):
        return unavailable


BOOK_TOOL_SCHEMA = {
    "name": "binary_book",
    "read_only": True,
    "version": "binary-book-v1",
    "description": (
        "Captured binary book integrity and counterfactual depth costs; "
        "no order action or fill probability."
    ),
    "input_schema": {
        "type": "object",
        "required": ["condition_id"],
        "properties": {
            "condition_id": {"type": "string", "pattern": "^0x[a-fA-F0-9]{64}$"},
            "shares": {"type": "string"},
            "as_of": {"type": "string"},
            "fee_bps": {"type": "string"},
            "fee_version": {"type": "string"},
            "side": {"enum": ["BUY", "SELL"]},
            "outcome": {"enum": ["YES", "NO"]},
        },
        "additionalProperties": False,
    },
}


def _tool_binary_book(args):
    return _run_async(binary_book_research(args))


TOOLS: dict[str, Any] = {
    "binary_book": _tool_binary_book,
    "describe_book_research": lambda _: BOOK_TOOL_SCHEMA,
    "web_search": _tool_web_search,
    "x_indexed_web_search": _tool_x_indexed_web_search,
    # Compatibility alias: the pre-rename name resolves to the same handler,
    # so clients that still ask for "x_posts_search" keep working.
    "x_posts_search": _tool_x_indexed_web_search,
    "tavily_search": _tool_tavily_search,
    "news_search": _tool_news_search,
    "cross_venue_markets": _tool_cross_venue_markets,
}

if _BINANCE_AVAILABLE:
    TOOLS.update(
        {
            "binance_smart_signals": _tool_binance_smart_signals,
            "binance_active_buy_signals": _tool_binance_active_buy_signals,
            "binance_social_hype": _tool_binance_social_hype,
            "binance_smart_money_inflow": _tool_binance_smart_money_inflow,
            "binance_trending_tokens": _tool_binance_trending_tokens,
            "binance_token_search": _tool_binance_token_search,
            "binance_token_data": _tool_binance_token_data,
            "binance_market_context": _tool_binance_market_context,
            "binance_pnl_leaderboard": _tool_binance_pnl_leaderboard,
        }
    )


def _run_one(payload: dict[str, Any]) -> dict[str, Any]:
    tool = str(payload.get("tool") or "").strip()
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    handler = TOOLS.get(tool)
    if handler is None:
        if tool.startswith("binance_") and not _BINANCE_AVAILABLE:
            return {"ok": False, "error": "Binance skills module unavailable"}
        return {"ok": False, "error": f"Unsupported tool: {tool}"}
    result = handler(args)
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
        return _rows_of(web_search(query, max_results=max_results))

    try:
        response = client.search(
            query=query,
            max_results=max(1, min(10, max_results)),
            search_depth=search_depth,  # "basic" (fast) or "advanced" (deeper)
        )
        results = []
        for item in response.get("results", []):
            results.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "url": str(item.get("url", "")).strip(),
                    "snippet": str(item.get("content", "")).strip(),
                    "score": str(item.get("score", "")),
                }
            )
        return results[: max(1, min(10, max_results))]
    except Exception:
        # Fallback to DuckDuckGo on failure
        return _rows_of(web_search(query, max_results=max_results))


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
        return _rows_of(web_search(f"{query} news", max_results=max_results))

    try:
        response = client.get_everything(
            q=query,
            language="en",
            sort_by=sort_by,  # relevancy, popularity, publishedAt
            page_size=max(1, min(20, max_results)),
        )
        results = []
        for article in response.get("articles", []):
            results.append(
                {
                    "title": str(article.get("title", "")).strip(),
                    "url": str(article.get("url", "")).strip(),
                    "snippet": str(article.get("description", "")).strip(),
                    "source": str((article.get("source") or {}).get("name", "")).strip(),
                    "published_at": str(article.get("publishedAt", "")).strip(),
                }
            )
        return results[: max(1, min(20, max_results))]
    except Exception:
        return _rows_of(web_search(f"{query} news", max_results=max_results))


def main() -> int:
    # ``__shutdown__`` lets a client that keeps stdin open close the loop
    # deliberately; the batched client in polymarket_mcp sends it as the last
    # frame of every call so a batch always ends in a clean exit.
    try:
        for line in sys.stdin:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Request must be an object")
                if payload.get("tool") == "__shutdown__":
                    sys.stdout.write(json.dumps({"ok": True, "result": {"shutdown": True}}) + "\n")
                    sys.stdout.flush()
                    break
                response = _run_one(payload)
            except Exception as exc:  # pragma: no cover
                response = {"ok": False, "error": str(exc)}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    finally:
        # Release the shared Binance HTTP client on shutdown (EOF or
        # ``__shutdown__``); idempotent, so it also covers an exception path.
        if _BINANCE_AVAILABLE:
            with contextlib.suppress(Exception):
                _run_async(binance_skills._close_shared_client())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
