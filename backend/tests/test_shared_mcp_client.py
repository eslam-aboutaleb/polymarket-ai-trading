"""Unit tests for the shared research MCP client.

This module is the single implementation used by both AI services, so these
tests also protect the services from silently diverging again. The transport is
stubbed throughout — no subprocess is ever spawned.
"""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "shared"))

from polymarket_mcp import ResearchMCPClient  # noqa: E402


class ServerPathResolutionTests(unittest.TestCase):
    """The client locates its server script across deployment layouts."""

    def test_env_var_wins_when_present(self):
        with (
            patch.dict(os.environ, {"RESEARCH_MCP_SERVER_PATH": "/custom/server.py"}),
            patch("pathlib.Path.exists", return_value=True),
        ):
            self.assertEqual(ResearchMCPClient().server_path, "/custom/server.py")

    def test_falls_back_to_container_path(self):
        with (
            patch.dict(os.environ, {"RESEARCH_MCP_SERVER_PATH": ""}),
            patch("pathlib.Path.exists", return_value=True),
        ):
            self.assertEqual(
                ResearchMCPClient().server_path,
                "/app/services/mcps/research/server.py",
            )

    def test_returns_empty_when_nothing_found(self):
        with (
            patch.dict(os.environ, {"RESEARCH_MCP_SERVER_PATH": ""}),
            patch("pathlib.Path.exists", return_value=False),
        ):
            self.assertEqual(ResearchMCPClient().server_path, "")

    def test_blank_env_var_is_ignored(self):
        with (
            patch.dict(os.environ, {"RESEARCH_MCP_SERVER_PATH": "   "}),
            patch("pathlib.Path.exists", return_value=False),
        ):
            self.assertEqual(ResearchMCPClient().server_path, "")


class CryptoTokenExtractionTests(unittest.TestCase):
    """Market titles are prose, so token symbols must survive normalisation."""

    def test_finds_symbols(self):
        self.assertEqual(ResearchMCPClient._extract_crypto_tokens("Will BTC reach 100k?"), ["BTC"])

    def test_maps_long_names(self):
        self.assertEqual(
            sorted(ResearchMCPClient._extract_crypto_tokens("Bitcoin or Ethereum")),
            ["BTC", "ETH"],
        )

    def test_strips_punctuation_and_currency(self):
        self.assertEqual(
            ResearchMCPClient._extract_crypto_tokens("Will $SOL pump? (maybe)"),
            ["SOL"],
        )

    def test_deduplicates_repeats(self):
        tokens = ResearchMCPClient._extract_crypto_tokens("BTC ETH BTC SOL")
        self.assertEqual(tokens, ["BTC", "ETH", "SOL"])

    def test_alias_and_symbol_collapse_to_one_entry(self):
        # "Bitcoin" and "BTC" both map to BTC.
        self.assertEqual(ResearchMCPClient._extract_crypto_tokens("Bitcoin BTC"), ["BTC"])

    def test_handles_apostrophes(self):
        self.assertEqual(
            ResearchMCPClient._extract_crypto_tokens("Will Uniswap's UNI token win?"),
            ["UNI"],
        )

    def test_caps_at_five(self):
        tokens = ResearchMCPClient._extract_crypto_tokens("BTC ETH SOL XRP ADA DOGE AVAX DOT")
        self.assertLessEqual(len(tokens), 5)

    def test_no_tokens_returns_empty(self):
        self.assertEqual(ResearchMCPClient._extract_crypto_tokens("Will it rain tomorrow?"), [])

    def test_empty_text_returns_empty(self):
        self.assertEqual(ResearchMCPClient._extract_crypto_tokens(""), [])


class SummarizeTests(unittest.TestCase):
    """Search rows are condensed into prompt-sized lines."""

    def test_empty_input(self):
        self.assertEqual(ResearchMCPClient._summarize([]), "")

    def test_joins_title_and_snippet(self):
        self.assertEqual(
            ResearchMCPClient._summarize([{"title": "T", "snippet": "S"}]),
            "T - S",
        )

    def test_appends_url_when_present(self):
        self.assertEqual(
            ResearchMCPClient._summarize([{"title": "T", "snippet": "S", "url": "http://u"}]),
            "T - S (http://u)",
        )

    def test_url_only_row(self):
        self.assertEqual(
            ResearchMCPClient._summarize([{"url": "http://u"}]),
            "http://u",
        )

    def test_missing_fields_are_skipped(self):
        self.assertEqual(ResearchMCPClient._summarize([{}]), "")

    def test_caps_at_four_rows(self):
        rows = [{"title": f"T{i}"} for i in range(10)]
        self.assertEqual(len(ResearchMCPClient._summarize(rows).splitlines()), 4)


class BinanceFormattingTests(unittest.TestCase):
    """Binance context is rendered into a prompt-ready block."""

    def _format(self, ctx):
        return ResearchMCPClient()._format_binance_for_prompt(ctx)

    def test_empty_context(self):
        self.assertEqual(self._format({}), "")

    def test_includes_all_three_sections(self):
        text = self._format(
            {
                "smart_money_signals_summary": "sig",
                "social_hype_summary": "hype",
                "smart_money_inflow_summary": "inflow",
            }
        )
        for expected in (
            "Binance Smart Money Signals",
            "Binance Social Hype",
            "Binance Smart Money Inflow",
        ):
            self.assertIn(expected, text)

    def test_skips_absent_sections(self):
        text = self._format({"social_hype_summary": "hype"})
        self.assertIn("Binance Social Hype", text)
        self.assertNotIn("Smart Money Signals", text)

    def test_renders_token_data(self):
        text = self._format(
            {"token_data": {"SOL": {"price": 150, "dynamic": {"volume_24h": 1000}}}}
        )
        self.assertIn("Binance Token Data", text)
        self.assertIn("SOL", text)
        self.assertIn("150", text)

    def test_unknown_fields_do_not_raise(self):
        # The upstream tool may grow new keys; rendering must not depend on them.
        self.assertIsInstance(self._format({"brand_new_field": "x"}), str)


class GatherBinanceContextTests(unittest.IsolatedAsyncioTestCase):
    """Binance enrichment is optional and must never break the caller."""

    def setUp(self):
        ResearchMCPClient._cache.clear()
        self.client = ResearchMCPClient()
        self.client.server_path = "/app/services/mcps/research/server.py"

    async def test_disabled_by_env_flag(self):
        with patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "false"}):
            result = await self.client._gather_binance_context("BTC")
        self.assertEqual(result, {"formatted": ""})

    async def test_case_insensitive_flag(self):
        with patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "FALSE"}):
            self.assertEqual(await self.client._gather_binance_context("BTC"), {"formatted": ""})

    async def test_formats_successful_payload(self):
        payload = {"social_hype_summary": "hype", "token_data": {}}
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "_call_tool", new=AsyncMock(return_value=payload)),
        ):
            result = await self.client._gather_binance_context("Will BTC rise?")
        self.assertIn("formatted", result)
        self.assertIn("hype", result["formatted"])

    async def test_non_dict_payload_is_ignored(self):
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "_call_tool", new=AsyncMock(return_value=[])),
        ):
            self.assertEqual(await self.client._gather_binance_context("BTC"), {"formatted": ""})

    async def test_tool_failure_degrades_gracefully(self):
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(
                self.client, "_call_tool", new=AsyncMock(side_effect=RuntimeError("boom"))
            ),
        ):
            self.assertEqual(await self.client._gather_binance_context("BTC"), {"formatted": ""})

    async def test_targets_solana(self):
        call = AsyncMock(return_value={})
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "_call_tool", new=call),
        ):
            await self.client._gather_binance_context("Will BTC rise?")
        self.assertEqual(call.await_args.args[1]["chain"], "solana")

    async def test_passes_extracted_tokens(self):
        call = AsyncMock(return_value={})
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "_call_tool", new=call),
        ):
            await self.client._gather_binance_context("Will Bitcoin rise?")
        self.assertEqual(call.await_args.args[1]["query_tokens"], ["BTC"])


class CallToolTests(unittest.IsolatedAsyncioTestCase):
    """The stdio transport degrades to empty results instead of raising."""

    def setUp(self):
        self.client = ResearchMCPClient()
        # Without a resolved server path the transport short-circuits before
        # spawning anything, so point it at a plausible location.
        self.client.server_path = "/app/services/mcps/research/server.py"

    def _proc(self, stdout: bytes, returncode: int = 0):
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    async def test_no_server_path_short_circuits(self):
        self.client.server_path = ""
        self.assertEqual(await self.client._call_tool("web_search", {}), [])

    async def _run(self, stdout, returncode=0):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=self._proc(stdout, returncode)),
        ):
            return await self.client._call_tool("web_search", {"query": "x"})

    async def test_parses_single_object(self):
        result = await self._run(b'{"ok": true, "result": {"a": 1}}\n')
        self.assertEqual(result, {"a": 1})

    async def test_filters_non_dict_rows_from_list(self):
        payload = b'{"ok": true, "result": [{"a": 1}, "junk", 5]}\n'
        self.assertEqual(await self._run(payload), [{"a": 1}])

    async def test_non_zero_exit_yields_empty(self):
        self.assertEqual(await self._run(b"", returncode=1), [])

    async def test_empty_output_yields_empty(self):
        self.assertEqual(await self._run(b""), [])

    async def test_error_payload_yields_empty(self):
        self.assertEqual(await self._run(b'{"ok": false, "error": "nope"}\n'), [])

    async def test_null_result_yields_empty(self):
        self.assertEqual(await self._run(b'{"ok": true, "result": null}\n'), [])

    async def test_scalar_result_yields_empty(self):
        self.assertEqual(await self._run(b'{"ok": true, "result": 42}\n'), [])

    async def test_malformed_json_yields_empty(self):
        self.assertEqual(await self._run(b"not json at all\n"), [])

    async def test_subprocess_failure_yields_empty(self):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("spawn failed")),
        ):
            self.assertEqual(await self.client._call_tool("web_search", {}), [])

    async def test_uses_last_line_of_output(self):
        payload = b'noise\n{"ok": true, "result": {"final": true}}\n'
        self.assertEqual(await self._run(payload), {"final": True})


class GatherMarketResearchTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end shape of the research bundle, with the cache verified."""

    def setUp(self):
        ResearchMCPClient._cache.clear()
        self.client = ResearchMCPClient()

    async def test_returns_all_four_sections(self):
        with (
            patch.object(self.client, "_call_tool", new=AsyncMock(return_value=[{"title": "T"}])),
            patch.object(
                self.client,
                "_gather_binance_context",
                new=AsyncMock(return_value={"formatted": "b"}),
            ),
        ):
            result = await self.client.gather_market_research("cid", "Will BTC rise?", "Yes", "No")
        for key in ("web_results", "x_results", "web_summary", "x_summary", "binance_context"):
            self.assertIn(key, result)
        self.assertEqual(result["binance_summary"], "b")

    async def test_second_call_is_cached(self):
        call = AsyncMock(return_value=[{"title": "T"}])
        with (
            patch.object(self.client, "_call_tool", new=call),
            patch.object(self.client, "_gather_binance_context", new=AsyncMock(return_value={})),
        ):
            first = await self.client.gather_market_research("cid", "T", "Yes", "No")
            second = await self.client.gather_market_research("cid", "T", "Yes", "No")
        self.assertIs(first, second)
        # Two tools per miss, and no further calls on the cache hit.
        self.assertEqual(call.await_count, 2)

    async def test_cache_keyed_on_condition_id(self):
        with (
            patch.object(self.client, "_call_tool", new=AsyncMock(return_value=[])),
            patch.object(self.client, "_gather_binance_context", new=AsyncMock(return_value={})),
        ):
            await self.client.gather_market_research("cid-a", "T", "Yes", "No")
            await self.client.gather_market_research("cid-b", "T", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 2)

    async def test_falls_back_to_title_when_no_condition_id(self):
        with (
            patch.object(self.client, "_call_tool", new=AsyncMock(return_value=[])),
            patch.object(self.client, "_gather_binance_context", new=AsyncMock(return_value={})),
        ):
            await self.client.gather_market_research("", "Unique title", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 1)

    async def test_all_empty_research_is_not_cached(self):
        # A transient outage (both searches empty, Binance failure sentinel)
        # must not poison the cache: the next identical call re-runs.
        with (
            patch.object(self.client, "_call_tool", new=AsyncMock(return_value=[])),
            patch.object(
                self.client,
                "_gather_binance_context",
                new=AsyncMock(return_value={"formatted": ""}),
            ),
        ):
            await self.client.gather_market_research("cid", "T", "Yes", "No")
            await self.client.gather_market_research("cid", "T", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 0)

    async def test_timed_out_research_is_not_cached(self):
        # A subprocess timeout returns an error payload; it must not be cached.
        with (
            patch.object(self.client, "_call_tool", new=AsyncMock(return_value=[])),
            patch.object(
                self.client,
                "_gather_binance_context",
                new=AsyncMock(return_value={"ok": False, "error": "research timed out"}),
            ),
        ):
            await self.client.gather_market_research("cid", "T", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 0)

    async def test_both_searches_share_query_args(self):
        call = AsyncMock(return_value=[])
        with (
            patch.object(self.client, "_call_tool", new=call),
            patch.object(self.client, "_gather_binance_context", new=AsyncMock(return_value={})),
        ):
            await self.client.gather_market_research(
                "cid", "Title", "Held", "Alt", recency_hours=6, max_results=3
            )
        self.assertEqual(call.await_count, 2)
        web_args = call.await_args_list[0].args[1]
        alt_args = call.await_args_list[1].args[1]
        self.assertEqual(web_args, alt_args)
        self.assertEqual(web_args["recency_hours"], 6)
        self.assertEqual(web_args["max_results"], 3)
        # The query folds in both outcomes.
        self.assertIn("Held", web_args["query"])
        self.assertIn("Alt", web_args["query"])


class ShimModuleTests(unittest.TestCase):
    """Both services must resolve to the same shared implementation."""

    def test_both_shims_reexport_the_shared_class(self):
        root = Path(__file__).resolve().parents[2] / "services"
        for service in ("llm-chain", "cli-agent"):
            shim = root / service / "src" / "mcp_client.py"
            self.assertTrue(shim.exists(), f"{service} shim missing")
            text = shim.read_text()
            self.assertIn("from polymarket_mcp import ResearchMCPClient", text)

    def test_no_service_still_contains_a_local_implementation(self):
        # Guards against someone re-inlining the client into one service.
        root = Path(__file__).resolve().parents[2] / "services"
        for service in ("llm-chain", "cli-agent"):
            text = (root / service / "src" / "mcp_client.py").read_text()
            self.assertNotIn("asyncio.create_subprocess_exec", text)

    def test_shared_package_is_importable(self):
        self.assertTrue(hasattr(importlib.import_module("polymarket_mcp"), "ResearchMCPClient"))


if __name__ == "__main__":
    unittest.main()
