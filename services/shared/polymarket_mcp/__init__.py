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
delimited JSON over stdio. A batch of N tool calls is served by **one**
subprocess: the client writes N request lines (plus a final ``__shutdown__``
frame), reads N response frames back in request order — the server answers in
order — and the shutdown frame makes the server stop reading and exit
deterministically instead of relying on EOF alone. Set
``RESEARCH_MCP_BATCH_ENABLED=false`` to fall back to one subprocess per call.

Timeouts are batch-wide, not per request: the whole batch runs under a single
wall-clock budget of ``_request_timeout_seconds`` × N. One hung tool therefore
spends the budget its siblings were counting on — when the budget is exhausted
every request in that batch, already answered or not, is replaced by the
timeout payload. Per-request budgets would require incremental reads between
writes, which deadlocks once a server's stdout fills the pipe while the client
is still writing, so the shared budget is a deliberate trade; its worst case
(60 s × N) is no worse than the one-subprocess-per-call path it replaced.

Results are bucketed into time-based in-process caches to avoid re-paying for
identical research inside a TTL window: ``gather_market_research`` caches the
whole bundle (per market) and the Binance context is cached separately for 15
minutes per extracted token tuple, because direct callers such as
``analysis_chain`` re-request it on every analysis. Only payloads that actually
carry data are cached — empty and error payloads (e.g. ``{"ok": false, "error":
"research timed out"}``) are never stored, so a transient outage cannot poison
the cache for the rest of the TTL window.

Binance enrichment is gated by the ``BINANCE_SKILLS_ENABLED`` env var, read
here on every call: that env read is the single gate for the AI path (the
llm-chain settings field that mirrored it was dead and has been deleted).
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

# Explicit shutdown frame understood by the research server: it answers with
# one acknowledgement and then stops reading, which is what makes a batched
# call terminate deterministically rather than depending on EOF.
_SHUTDOWN_TOOL = "__shutdown__"

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

# One inclusive truthy rule, shared by every env gate below so the gates
# cannot drift apart (one of them used to accept only the bare string
# "true"). Every gate defaults to enabled, and the explicit falsy spellings
# ("false", "0", "no", "off", unset-but-blank) all disable.
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: bool = True) -> bool:
    """Read a boolean env gate, applying the shared truthy rule.

    Args:
        name: Environment variable to read.
        default: Value used when the variable is unset.

    Returns:
        True when the variable is unset and ``default`` is true, or when it
        holds any of the accepted truthy spellings; false otherwise.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY_VALUES


class ResearchMCPClient:
    """Calls the open-source research MCP server over stdio.

    All public methods are resilient by design: research enrichment is a
    best-effort addition to an LLM prompt, so any transport, decoding or
    subprocess failure yields an empty result rather than propagating into the
    analysis path.
    """

    # Class-level, shared by every instance. Safe for the current
    # deployments because each one runs a single process whose single asyncio
    # event loop is the only writer, and because keys are content-derived, so
    # two instances asking the same question share one entry. It is *not*
    # shared between processes — every worker keeps its own copy. Bounded
    # (LRU) so a long-running service cannot grow it without limit.
    # Key: (condition_id or title, TTL bucket, recency_hours, max_results).
    _cache: OrderedDict[tuple[str, int, int, int], dict[str, Any]] = OrderedDict()
    _cache_max_entries = 256
    _ttl_seconds = 15 * 60

    # Separate Binance-context cache, keyed by (extracted token tuple, TTL
    # bucket). Direct callers (e.g. ``analysis_chain``) ask for it on every
    # analysis, so without it each call would re-spawn a subprocess for data
    # that only changes a few times a day. Same single-event-loop guarantee
    # and the same per-process scope as ``_cache`` above.
    _binance_cache: OrderedDict[tuple[tuple[str, ...], int], dict[str, Any]] = OrderedDict()
    _binance_cache_max_entries = 256

    # Wall-clock budget for one batched subprocess call. It scales with the
    # batch size because it covers the whole batch, not each request: a single
    # hung tool spends the shared budget and discards its siblings' answers.
    _request_timeout_seconds = 60.0

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
            # services/shared/polymarket_mcp -> services
            str(Path(__file__).resolve().parents[2] / "mcps" / "research" / "server.py"),
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

        # Every uncached tool call travels in a single subprocess: a cold market
        # costs 1 spawn instead of 3 (web, X, Binance). Binance context that is
        # still inside its 15-minute cache is simply reused.
        requests: list[tuple[str, dict[str, Any]]] = [
            ("web_search", search_args),
            ("x_posts_search", search_args),
        ]
        binance_key: tuple[tuple[str, ...], int] | None = None
        binance_context: dict[str, Any] | None = None
        if self._binance_enabled():
            binance_key = self._binance_cache_key(self._extract_crypto_tokens(market_title))
            binance_context = self._binance_cache_get(binance_key)
            if binance_context is None:
                requests.append(
                    (
                        "binance_market_context",
                        {"query_tokens": list(binance_key[0]), "chain": _BINANCE_CHAIN},
                    )
                )

        if self._batch_enabled():
            responses = await self.call_tools(requests)
        else:
            # Transport kill-switch fallback: one subprocess per tool call.
            responses = [await self._call_tool(tool, args) for tool, args in requests]

        web_results, x_results = responses[0], responses[1]
        if binance_key is not None and binance_context is None:
            binance_context = self._binance_context_from_payload(responses[2])
            self._binance_cache_put(binance_key, binance_context)
        if binance_context is None:
            binance_context = {"formatted": ""}

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
        targets the assets actually named by the market. The result is cached
        for ``_ttl_seconds`` per extracted token tuple, so repeated analyses of
        markets naming the same assets do not re-spawn the MCP server.

        Args:
            market_title: Market question text to mine for token symbols.

        Returns:
            The raw context dict plus a ``formatted`` prompt-ready string.
            ``{"formatted": ""}`` when disabled or unavailable.
        """
        if not self._binance_enabled():
            return {"formatted": ""}

        cache_key = self._binance_cache_key(self._extract_crypto_tokens(market_title))
        cached = self._binance_cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            raw = await self._call_tool(
                "binance_market_context",
                {"query_tokens": list(cache_key[0]), "chain": _BINANCE_CHAIN},
            )
        except Exception as exc:
            logger.debug("Binance context gathering failed: %s", exc)
            return {"formatted": ""}

        context = self._binance_context_from_payload(raw)
        self._binance_cache_put(cache_key, context)
        return context

    @staticmethod
    def _binance_enabled() -> bool:
        """Single AI-path gate for Binance enrichment, read from the environment."""
        return _env_flag("BINANCE_SKILLS_ENABLED")

    def _binance_context_from_payload(self, raw: Any) -> dict[str, Any]:
        """Turn a raw ``binance_market_context`` payload into a prompt context.

        Args:
            raw: Tool result as returned by the research server.

        Returns:
            The raw payload plus a ``formatted`` string, or the empty sentinel
            when the payload is not a dict.
        """
        if isinstance(raw, dict):
            return {**raw, "formatted": self._format_binance_for_prompt(raw)}
        return {"formatted": ""}

    @classmethod
    def _binance_cache_key(cls, tokens: list[str]) -> tuple[tuple[str, ...], int]:
        """Build the 15-minute-bucket cache key for an extracted token list."""
        return (tuple(tokens), int(time.time() // cls._ttl_seconds))

    @classmethod
    def _binance_cache_get(cls, key: tuple[tuple[str, ...], int]) -> dict[str, Any] | None:
        """Return the cached Binance context for a key, if the bucket is live."""
        entry = cls._binance_cache.get(key)
        if entry:
            cls._binance_cache.move_to_end(key)
            return entry
        return None

    @classmethod
    def _binance_cache_put(cls, key: tuple[tuple[str, ...], int], context: dict[str, Any]) -> None:
        """Store usable Binance context, bounded (LRU).

        Empty and error payloads are deliberately not cached: a disabled
        integration, a failed call or a timed-out server must not stick for
        the rest of the TTL bucket.
        """
        if not context or context.get("error") or not context.get("formatted"):
            return
        cls._binance_cache[key] = context
        cls._binance_cache.move_to_end(key)
        while len(cls._binance_cache) > cls._binance_cache_max_entries:
            cls._binance_cache.popitem(last=False)

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

    @staticmethod
    def _batch_enabled() -> bool:
        """Read the transport kill-switch (on by default).

        ``RESEARCH_MCP_BATCH_ENABLED=false`` falls back to one subprocess per
        tool call, which is how to disable the batching transport in one env
        var if it ever misbehaves in a deployment. Shares :func:`_env_flag`,
        so both gates accept the same spellings of on and off.
        """
        return _env_flag("RESEARCH_MCP_BATCH_ENABLED")

    async def call_tools(self, requests: list[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Invoke several tools in a single research-server subprocess.

        One process serves the whole batch: N request lines are written, the
        server answers each in order, a final ``__shutdown__`` frame makes it
        stop reading, and the N responses are returned in request order.

        The batch runs under one shared wall-clock budget
        (``_request_timeout_seconds`` × N). It is not a per-request budget: if
        a single tool hangs, the budget it consumes also costs its
        already-answered siblings, because their frames are discarded together
        with the timeout payload.

        Args:
            requests: ``(tool, args)`` pairs, executed in the given order.

        Returns:
            One entry per request. A request whose server reply is missing,
            malformed or an error payload yields an empty container; a batch
            that exhausts its shared budget yields the transport timeout
            payload for every request, including the answered ones. When no
            server script is resolvable every entry is empty.
        """
        if not requests:
            return []
        if not self.server_path:
            return [[] for _ in requests]

        payload = "".join(
            json.dumps({"tool": tool, "args": args}) + "\n"
            for tool, args in (*requests, (_SHUTDOWN_TOOL, {}))
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                self.server_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(payload.encode("utf-8")),
                    timeout=self._request_timeout_seconds * len(requests),
                )
            except TimeoutError:
                # A hung server must never block the LLM request forever.
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=5)
                logger.debug(
                    "MCP batch call timed out after %.0fs (%d requests)",
                    self._request_timeout_seconds * len(requests),
                    len(requests),
                )
                return [{"ok": False, "error": "research timed out"} for _ in requests]
            finally:
                # Reap the child on *every* exit path, not just the timeout
                # one: a cancelled batch (asyncio.CancelledError) or any other
                # failure would otherwise abandon a process that still holds
                # the stdio pipes open. After a clean communicate() the
                # return code is already set, so this is a no-op there.
                if proc.returncode is None:
                    proc.kill()
            logger.debug("MCP server stderr: %s", stderr)
            if proc.returncode != 0:
                return [[] for _ in requests]

            return self._decode_response_frames(stdout.decode("utf-8"), len(requests))
        except Exception:
            logger.debug("MCP batch call failed", exc_info=True)
            return [[] for _ in requests]

    @classmethod
    def _decode_response_frames(cls, raw_stdout: str, expected: int) -> list[Any]:
        """Decode the first ``expected`` JSON response frames from server stdout.

        The server writes exactly one JSON object per request line; any other
        stdout line is noise (a stray import warning, for instance) and is
        skipped rather than being mistaken for a frame.

        Args:
            raw_stdout: Everything the server wrote to stdout.
            expected: Number of response frames to return.

        Returns:
            One decoded entry per request, in request order. Requests with no
            usable frame yield an empty container.
        """
        responses: list[Any] = []
        for line in raw_stdout.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            responses.append(cls._decode_response(parsed))
            if len(responses) == expected:
                return responses
        responses.extend([] for _ in range(expected - len(responses)))
        return responses

    @staticmethod
    def _decode_response(parsed: dict[str, Any]) -> Any:
        """Convert one server response object into its tool result.

        Args:
            parsed: A parsed ``{"ok": ..., "result": ...}`` response object.

        Returns:
            The result (dict or list of dicts), or an empty container when the
            call failed or the payload is unusable.
        """
        if not parsed.get("ok"):
            return []
        result = parsed.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return [row for row in result if isinstance(row, dict)]
        return []

    async def _call_tool(self, tool: str, args: dict[str, Any]) -> list[dict[str, str]] | dict:
        """Invoke one tool on the research MCP server.

        Delegates to :meth:`call_tools`, so the single-call path is the same
        transport as the batched one (one subprocess, one request frame, one
        response frame) with identical degradation behaviour.

        Args:
            tool: Tool name understood by the server.
            args: JSON-serialisable arguments for the tool.

        Returns:
            The tool result (dict or list of dicts), or an empty container when
            the server is missing, exits non-zero, or returns an error payload.
        """
        if not self.server_path:
            return []
        responses = await self.call_tools([(tool, args)])
        return responses[0] if responses else []

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
