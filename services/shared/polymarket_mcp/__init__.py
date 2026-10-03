"""Shared MCP client used by both Polymarket AI microservices.

``llm-chain`` and ``cli-agent`` both need to enrich LLM prompts with external
research (web search, X/Twitter posts, Binance smart-money flows). The two
services previously carried byte-near-identical copies of that client, which
meant every fix had to be applied twice and the copies had already drifted
(the ``cli-agent`` copy was missing several token aliases).

This package is the single canonical implementation. Both services import it;
their own ``src/mcp_client.py`` modules are thin re-export shims so existing
call sites (``from src.mcp_client import ResearchMCPClient``) keep working.

Transport
---------
The research MCP server is an ordinary Python script that speaks newline
delimited JSON over stdio. Each call spawns a short-lived subprocess, writes one
request line, and reads the final response line back. Results are bucketed into
a time-based in-process cache to avoid re-paying for identical research within
a TTL window.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["ResearchMCPClient"]

# Candidate locations for the research MCP server, tried in order. The env var
# wins so a deployment can point at an absolute path; the remaining entries
# cover the container layout and a local checkout.
_DEFAULT_SERVER_PATHS = (
    "/app/services/mcps/research/server.py",
    "services/mcps/research/server.py",
)

# Token symbols that may appear in a prediction-market title, and the
# long-form names that map onto them. Titles are written in prose
# ("Will Bitcoin hit $100k?"), so both spellings must be recognised.
_KNOWN_TOKENS = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "BNB",
        "XRP",
        "ADA",
        "DOGE",
        "AVAX",
        "DOT",
        "MATIC",
        "LINK",
        "UNI",
        "ATOM",
        "LTC",
        "FIL",
        "APT",
        "ARB",
        "OP",
        "SUI",
        "SEI",
        "TIA",
        "JUP",
        "WIF",
        "BONK",
        "PEPE",
        "SHIB",
        "NEAR",
        "INJ",
        "TRX",
        "TON",
        "RENDER",
        "FET",
        "TAO",
        "BITCOIN",
        "ETHEREUM",
        "SOLANA",
    }
)

_NAME_TO_SYMBOL = {
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "SOLANA": "SOL",
    "DOGECOIN": "DOGE",
    "CARDANO": "ADA",
    "POLKADOT": "DOT",
    "POLYGON": "MATIC",
    "CHAINLINK": "LINK",
    "UNISWAP": "UNI",
    "LITECOIN": "LTC",
    "FILECOIN": "FIL",
    "ARBITRUM": "ARB",
    "OPTIMISM": "OP",
    "AVALANCHE": "AVAX",
    "COSMOS": "ATOM",
    "RIPPLE": "XRP",
    "TONCOIN": "TON",
}

# Binance lookups are always performed against Solana markets, which is where
# the highest-signal smart-money flow data lives.
_BINANCE_CHAIN = "solana"


class ResearchMCPClient:
    """Calls the open-source research MCP server over stdio.

    All public methods are resilient by design: research enrichment is a
    best-effort addition to an LLM prompt, so any transport, decoding or
    subprocess failure yields an empty result rather than propagating into the
    analysis path.
    """

    # Shared across instances: both services are single-process, and the cache
    # is keyed by content so cross-instance sharing is safe and desirable.
    # Bounded (LRU) so a long-running service cannot grow it without limit.
    # Key: (condition_id or title, TTL bucket, recency_hours, max_results).
    _cache: OrderedDict[tuple[str, int, int, int], dict[str, Any]] = OrderedDict()
    _cache_max_entries = 256
    _ttl_seconds = 15 * 60

    def __init__(self) -> None:
        self.server_path = self._resolve_server_path()

    def _resolve_server_path(self) -> str:
        """Locate the research MCP server script.

        Returns:
            Absolute path to the server, or an empty string when none of the
            candidates exist — callers degrade to "no research" in that case.
        """
        env_path = os.getenv("RESEARCH_MCP_SERVER_PATH", "").strip()
        candidates = [
            env_path,
            *_DEFAULT_SERVER_PATHS,
            # services/shared/polymarket_mcp -> repo root
            str(Path(__file__).resolve().parents[3] / "mcps" / "research" / "server.py"),
            str(Path.cwd() / "services" / "mcps" / "research" / "server.py"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return ""

    async def gather_market_research(
        self,
        condition_id: str,
        market_title: str,
        held_outcome: str,
        best_alt_outcome: str,
        recency_hours: int = 24,
        max_results: int = 8,
    ) -> dict[str, Any]:
        """Collect web, social and on-chain context for one market.

        Results are cached in fixed time buckets so repeated analysis of the
        same market inside a TTL window is free.

        Args:
            condition_id: Polymarket condition id, used as the cache key.
            market_title: Human-readable market question.
            held_outcome: Outcome currently held by the user.
            best_alt_outcome: Best alternative outcome, for contrast searches.
            recency_hours: How recent the search results must be.
            max_results: Maximum results requested per search.

        Returns:
            Dict with raw and summarised results for each source. Empty values
            are returned when a source is unavailable.
        """
        bucket = int(time.time() // self._ttl_seconds)
        key = (condition_id or market_title, bucket, recency_hours, max_results)
        cached = self._cache.get(key)
        if cached:
            self._cache.move_to_end(key)
            return cached

        query = f"{market_title} {held_outcome} {best_alt_outcome}".strip()
        search_args = {
            "query": query,
            "recency_hours": recency_hours,
            "max_results": max_results,
        }
        web_results = await self._call_tool("web_search", search_args)
        x_results = await self._call_tool("x_posts_search", search_args)

        # ── Binance Smart Money + Sentiment context ──────────
        binance_context = await self._gather_binance_context(market_title)

        result = {
            "web_results": web_results,
            "x_results": x_results,
            "web_summary": self._summarize(web_results),
            "x_summary": self._summarize(x_results),
            "binance_context": binance_context,
            "binance_summary": binance_context.get("formatted", ""),
        }
        # Cache only when at least one source produced data, so a transient
        # DDG/Binance outage is not poisoned for the rest of the TTL bucket.
        # Web/X searches yield non-empty lists on success; the Binance context
        # is the bare {"formatted": ""} sentinel when enrichment is disabled
        # or every Binance source failed, and error payloads (e.g. a timed-out
        # server) carry an "error" key — neither counts as data.
        has_data = (
            (isinstance(web_results, list) and bool(web_results))
            or (isinstance(x_results, list) and bool(x_results))
            or (binance_context != {"formatted": ""} and not binance_context.get("error"))
        )
        if has_data:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)
        return result

    async def _gather_binance_context(self, market_title: str) -> dict[str, Any]:
        """Fetch Binance smart-money signals, social hype and inflow data.

        Crypto token symbols are extracted from the market title so the lookup
        targets the assets actually named by the market.

        Args:
            market_title: Market question text to mine for token symbols.

        Returns:
            The raw context dict plus a ``formatted`` prompt-ready string.
            ``{"formatted": ""}`` when disabled or unavailable.
        """
        if os.getenv("BINANCE_SKILLS_ENABLED", "true").strip().lower() != "true":
            return {"formatted": ""}

        query_tokens = self._extract_crypto_tokens(market_title)

        try:
            raw = await self._call_tool(
                "binance_market_context",
                {"query_tokens": query_tokens, "chain": _BINANCE_CHAIN},
            )
            if isinstance(raw, dict):
                return {**raw, "formatted": self._format_binance_for_prompt(raw)}
            return {"formatted": ""}
        except Exception as exc:
            logger.debug("Binance context gathering failed: %s", exc)
            return {"formatted": ""}

    def _format_binance_for_prompt(self, ctx: dict[str, Any]) -> str:
        """Format Binance context into a readable string for LLM prompts.

        Args:
            ctx: Context dict returned by the ``binance_market_context`` tool.

        Returns:
            Concatenated sections, or an empty string when nothing is present.
        """
        sections = [
            ("smart_money_signals_summary", "Binance Smart Money Signals"),
            ("social_hype_summary", "Binance Social Hype"),
            ("smart_money_inflow_summary", "Binance Smart Money Inflow"),
        ]
        parts = [
            f"== {label} ==\n{summary}" for key, label in sections if (summary := ctx.get(key, ""))
        ]

        token_data = ctx.get("token_data", {})
        if token_data:
            lines = ["== Binance Token Data =="]
            for symbol, data in token_data.items():
                dynamic = data.get("dynamic", {})
                lines.append(
                    f"• {symbol}: ${data.get('price', '?')} "
                    f"(24h: {dynamic.get('price_change_24h', '?')}%) "
                    f"Vol: ${dynamic.get('volume_24h', '?')} "
                    f"SmartMoney: {dynamic.get('smart_money_holders', '?')} holders"
                )
            parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _extract_crypto_tokens(text: str) -> list[str]:
        """Extract likely crypto token symbols from market title text.

        Args:
            text: Market title or description text.

        Returns:
            Up to five de-duplicated ticker symbols, in order of appearance.
        """
        words = text.upper().replace(",", " ").replace("?", " ").replace("'", " ").split()
        found: list[str] = []
        for word in words:
            clean = word.strip("$().!?")
            if clean in _KNOWN_TOKENS:
                mapped = _NAME_TO_SYMBOL.get(clean, clean)
                if mapped not in found:
                    found.append(mapped)
            elif (mapped := _NAME_TO_SYMBOL.get(clean)) and mapped not in found:
                found.append(mapped)
        return found[:5]

    async def _call_tool(self, tool: str, args: dict[str, Any]) -> list[dict[str, str]] | dict:
        """Invoke one tool on the research MCP server.

        Args:
            tool: Tool name understood by the server.
            args: JSON-serialisable arguments for the tool.

        Returns:
            The tool result (dict or list of dicts), or an empty container when
            the server is missing, exits non-zero, or returns an error payload.
        """
        if not self.server_path:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                self.server_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            payload = json.dumps({"tool": tool, "args": args}) + "\n"
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(payload.encode("utf-8")), timeout=60
                )
            except TimeoutError:
                # A hung server must never block the LLM request forever.
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=5)
                logger.debug("MCP tool call %r timed out after 60s", tool)
                return {"ok": False, "error": "research timed out"}
            logger.debug("MCP server stderr: %s", stderr)
            if proc.returncode != 0:
                return []

            lines = stdout.decode("utf-8").strip().splitlines()
            if not lines:
                return []

            parsed = json.loads(lines[-1])
            if not parsed.get("ok"):
                return []

            result = parsed.get("result")
            if isinstance(result, dict):
                return result
            if isinstance(result, list):
                return [row for row in result if isinstance(row, dict)]
            return []
        except Exception:
            logger.debug("MCP tool call %r failed", tool, exc_info=True)
            return []

    @staticmethod
    def _summarize(results: list[dict[str, str]]) -> str:
        """Condense search results into one line per result.

        Args:
            results: Raw rows returned by a search tool.

        Returns:
            Newline-separated ``title - snippet (url)`` strings, capped at four
            rows, or an empty string when there are no usable results.
        """
        if not isinstance(results, list):
            return ""
        if not results:
            return ""
        lines: list[str] = []
        for row in results[:4]:
            title = str(row.get("title") or "").strip()
            snippet = str(row.get("snippet") or "").strip()
            url = str(row.get("url") or "").strip()
            part = " - ".join(p for p in [title, snippet] if p)
            if url:
                part = f"{part} ({url})" if part else url
            if part:
                lines.append(part)
        return "\n".join(lines)
