"""Minimal stdio MCP client for research tools."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ResearchMCPClient:
    """Calls the open-source research MCP server over stdio."""

    _cache: dict[tuple[str, int], dict[str, Any]] = {}
    _ttl_seconds = 15 * 60

    def __init__(self):
        self.server_path = self._resolve_server_path()

    def _resolve_server_path(self) -> str:
        env_path = os.getenv("RESEARCH_MCP_SERVER_PATH", "").strip()
        candidates = [
            env_path,
            "/app/services/mcps/research/server.py",
            str(Path(__file__).resolve().parents[2] / "mcps" / "research" / "server.py"),
            str(Path.cwd() / "services" / "mcps" / "research" / "server.py"),
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
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
        bucket = int(time.time() // self._ttl_seconds)
        key = (condition_id or market_title, bucket)
        cached = self._cache.get(key)
        if cached:
            return cached

        query = f'{market_title} {held_outcome} {best_alt_outcome}'.strip()
        web_results = await self._call_tool(
            "web_search",
            {
                "query": query,
                "recency_hours": recency_hours,
                "max_results": max_results,
            },
        )
        x_results = await self._call_tool(
            "x_posts_search",
            {
                "query": query,
                "recency_hours": recency_hours,
                "max_results": max_results,
            },
        )

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
        self._cache[key] = result
        return result

    async def _gather_binance_context(self, market_title: str) -> dict[str, Any]:
        """
        Fetch Binance smart money signals, social hype, and inflow data.
        Extracts crypto token symbols from the market title to do targeted lookups.
        """
        binance_enabled = os.getenv("BINANCE_SKILLS_ENABLED", "true").strip().lower()
        if binance_enabled != "true":
            return {"formatted": ""}

        # Extract potential crypto tokens from the market title
        query_tokens = self._extract_crypto_tokens(market_title)

        try:
            raw = await self._call_tool(
                "binance_market_context",
                {
                    "query_tokens": query_tokens,
                    "chain": "solana",
                },
            )
            if isinstance(raw, dict):
                # The tool returns the full context dict
                formatted = self._format_binance_for_prompt(raw)
                return {**raw, "formatted": formatted}
            return {"formatted": ""}
        except Exception as exc:
            logger.debug(f"Binance context gathering failed: {exc}")
            return {"formatted": ""}

    def _format_binance_for_prompt(self, ctx: dict[str, Any]) -> str:
        """Format Binance context into a readable string for LLM prompts."""
        parts = []

        signals_summary = ctx.get("smart_money_signals_summary", "")
        if signals_summary:
            parts.append(f"== Binance Smart Money Signals ==\n{signals_summary}")

        hype_summary = ctx.get("social_hype_summary", "")
        if hype_summary:
            parts.append(f"== Binance Social Hype ==\n{hype_summary}")

        inflow_summary = ctx.get("smart_money_inflow_summary", "")
        if inflow_summary:
            parts.append(f"== Binance Smart Money Inflow ==\n{inflow_summary}")

        token_data = ctx.get("token_data", {})
        if token_data:
            lines = ["== Binance Token Data =="]
            for symbol, data in token_data.items():
                dyn = data.get("dynamic", {})
                lines.append(
                    f"• {symbol}: ${data.get('price', '?')} "
                    f"(24h: {dyn.get('price_change_24h', '?')}%) "
                    f"Vol: ${dyn.get('volume_24h', '?')} "
                    f"SmartMoney: {dyn.get('smart_money_holders', '?')} holders"
                )
            parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _extract_crypto_tokens(text: str) -> list[str]:
        """Extract likely crypto token symbols from market title text."""
        # Common crypto tokens that appear in prediction market titles
        KNOWN_TOKENS = {
            "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX",
            "DOT", "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "APT",
            "ARB", "OP", "SUI", "SEI", "TIA", "JUP", "WIF", "BONK",
            "PEPE", "SHIB", "NEAR", "INJ", "TRX", "TON", "RENDER",
            "FET", "TAO", "BITCOIN", "ETHEREUM", "SOLANA",
        }
        # Normalize: "Bitcoin" -> "BTC", "Ethereum" -> "ETH", etc.
        NAME_TO_SYMBOL = {
            "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL",
            "DOGECOIN": "DOGE", "CARDANO": "ADA", "POLKADOT": "DOT",
            "POLYGON": "MATIC", "CHAINLINK": "LINK", "UNISWAP": "UNI",
            "LITECOIN": "LTC", "FILECOIN": "FIL", "ARBITRUM": "ARB",
            "OPTIMISM": "OP", "AVALANCHE": "AVAX", "COSMOS": "ATOM",
            "RIPPLE": "XRP", "TONCOIN": "TON",
        }
        words = text.upper().replace(",", " ").replace("?", " ").replace("'", " ").split()
        found = []
        for w in words:
            clean = w.strip("$().!?")
            if clean in KNOWN_TOKENS and clean not in found:
                mapped = NAME_TO_SYMBOL.get(clean, clean)
                if mapped not in found:
                    found.append(mapped)
            elif clean in NAME_TO_SYMBOL and NAME_TO_SYMBOL[clean] not in found:
                found.append(NAME_TO_SYMBOL[clean])
        return found[:5]

    async def _call_tool(self, tool: str, args: dict[str, Any]) -> list[dict[str, str]] | dict[str, Any]:
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
            stdout, _ = await proc.communicate(payload.encode("utf-8"))
            if proc.returncode != 0:
                return []
            line = stdout.decode("utf-8").strip().splitlines()
            if not line:
                return []
            parsed = json.loads(line[-1])
            if not parsed.get("ok"):
                return []
            result = parsed.get("result")
            if result is None:
                return []
            # Return dicts or lists as-is
            if isinstance(result, dict):
                return result
            if isinstance(result, list):
                return [r for r in result if isinstance(r, dict)]
            return []
        except Exception:
            return []

    @staticmethod
    def _summarize(results: list[dict[str, str]]) -> str:
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
