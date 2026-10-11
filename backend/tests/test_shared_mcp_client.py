"""Unit tests for the shared research MCP client.

This module is the single implementation used by both AI services, so these
tests also protect the services from silently diverging again. The transport is
stubbed throughout — no subprocess is ever spawned.
"""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "shared"))

from polymarket_mcp import ResearchMCPClient, _env_flag  # noqa: E402

# Transport doubles shared with test_mcp_transport.py so the two modules do not
# each keep their own copy of the same stub (duplication budget).
SERVER_PATH = "/app/services/mcps/research/server.py"


def make_proc(stdout: bytes, returncode: int = 0):
    """Build a subprocess double whose ``communicate`` returns ``stdout``."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


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

    def test_source_relative_candidate_lands_inside_the_repo(self):
        # ``parents[3]`` used to point one level above the repo, so that
        # candidate was dead and only the cwd fallback covered local dev.
        # ``parents[2]`` is ``services``, where the server really lives.
        expected = (
            Path(__file__).resolve().parents[2] / "services" / "mcps" / "research" / "server.py"
        )
        self.assertTrue(expected.exists(), f"{expected} missing")
        with (
            patch.dict(os.environ, {"RESEARCH_MCP_SERVER_PATH": ""}),
            patch(
                "polymarket_mcp.Path.cwd",
                return_value=Path(tempfile.gettempdir(), "not-this-repo"),
            ),
        ):
            self.assertEqual(ResearchMCPClient().server_path, str(expected))


class EnvGateTests(unittest.TestCase):
    """Both env gates share one truthy rule instead of drifting apart."""

    GATES = (
        ("BINANCE_SKILLS_ENABLED", ResearchMCPClient._binance_enabled),
        ("RESEARCH_MCP_BATCH_ENABLED", ResearchMCPClient._batch_enabled),
    )

    def test_common_truthy_spellings_enable_every_gate(self):
        for raw in ("1", "true", "TRUE", "yes", "on", " on "):
            for env_name, gate in self.GATES:
                with patch.dict(os.environ, {env_name: raw}):
                    self.assertTrue(gate(), f"{env_name}={raw!r} should enable")

    def test_falsy_spellings_disable_every_gate(self):
        for raw in ("0", "false", "False", "no", "off", "", "   "):
            for env_name, gate in self.GATES:
                with patch.dict(os.environ, {env_name: raw}):
                    self.assertFalse(gate(), f"{env_name}={raw!r} should disable")

    def test_unset_gate_defaults_to_enabled(self):
        with patch.dict(os.environ):
            for env_name, gate in self.GATES:
                os.environ.pop(env_name, None)
                self.assertTrue(gate(), f"{env_name} should default to on")
            os.environ.pop("SOME_UNSET_FLAG", None)
            self.assertTrue(_env_flag("SOME_UNSET_FLAG"))
            self.assertFalse(_env_flag("SOME_UNSET_FLAG", default=False))


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
        ResearchMCPClient._binance_cache.clear()
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
        self.client.server_path = SERVER_PATH

    async def test_no_server_path_short_circuits(self):
        self.client.server_path = ""
        self.assertEqual(await self.client._call_tool("web_search", {}), [])

    async def _run(self, stdout, returncode=0):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=make_proc(stdout, returncode)),
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

    async def test_uses_first_json_frame_of_output(self):
        # The transport decodes the first JSON frame on stdout; leading noise
        # (a stray import warning) is skipped, trailing lines are ignored.
        payload = b'noise\n{"ok": true, "result": {"final": true}}\n'
        self.assertEqual(await self._run(payload), {"final": True})


class GatherMarketResearchTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end shape of the research bundle, with the cache verified."""

    def setUp(self):
        ResearchMCPClient._cache.clear()
        ResearchMCPClient._binance_cache.clear()
        self.client = ResearchMCPClient()

    async def test_returns_all_four_sections(self):
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(
                self.client,
                "call_tools",
                new=AsyncMock(
                    return_value=[
                        [{"title": "T"}],
                        [{"title": "T"}],
                        {"social_hype_summary": "hype"},
                    ]
                ),
            ),
        ):
            result = await self.client.gather_market_research("cid", "Will BTC rise?", "Yes", "No")
        for key in ("web_results", "x_results", "web_summary", "x_summary", "binance_context"):
            self.assertIn(key, result)
        self.assertIn("hype", result["binance_summary"])

    async def test_second_call_is_cached(self):
        batch = AsyncMock(
            return_value=[[{"title": "T"}], [{"title": "T"}], {"social_hype_summary": "hype"}]
        )
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            first = await self.client.gather_market_research("cid", "T", "Yes", "No")
            second = await self.client.gather_market_research("cid", "T", "Yes", "No")
        self.assertIs(first, second)
        # One batch on the miss, and no further call on the cache hit.
        self.assertEqual(batch.await_count, 1)

    async def test_cache_keyed_on_condition_id(self):
        batch = AsyncMock(
            return_value=[[{"title": "T"}], [{"title": "T"}], {"social_hype_summary": "hype"}]
        )
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            await self.client.gather_market_research("cid-a", "T", "Yes", "No")
            await self.client.gather_market_research("cid-b", "T", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 2)

    async def test_falls_back_to_title_when_no_condition_id(self):
        batch = AsyncMock(
            return_value=[[{"title": "T"}], [{"title": "T"}], {"social_hype_summary": "hype"}]
        )
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            await self.client.gather_market_research("", "Unique title", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 1)

    async def test_all_empty_research_is_not_cached(self):
        # A transient outage (both searches empty, Binance failure sentinel)
        # must not poison the cache: the next identical call re-runs.
        batch = AsyncMock(return_value=[[], [], {"formatted": ""}])
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            await self.client.gather_market_research("cid", "T", "Yes", "No")
            await self.client.gather_market_research("cid", "T", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 0)

    async def test_timed_out_research_is_not_cached(self):
        # A subprocess timeout returns an error payload; it must not be cached.
        batch = AsyncMock(return_value=[[], [], {"ok": False, "error": "research timed out"}])
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            await self.client.gather_market_research("cid", "T", "Yes", "No")
        self.assertEqual(len(ResearchMCPClient._cache), 0)

    async def test_both_searches_share_query_args(self):
        batch = AsyncMock(return_value=[[], [], {"formatted": ""}])
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            await self.client.gather_market_research(
                "cid", "Title", "Held", "Alt", recency_hours=6, max_results=3
            )
        # One batch for the cold market, carrying both searches in order.
        self.assertEqual(batch.await_count, 1)
        requested = batch.await_args.args[0]
        self.assertEqual([tool for tool, _ in requested][:2], ["web_search", "x_posts_search"])
        web_args = requested[0][1]
        alt_args = requested[1][1]
        self.assertEqual(web_args, alt_args)
        self.assertEqual(web_args["recency_hours"], 6)
        self.assertEqual(web_args["max_results"], 3)
        # The query folds in both outcomes.
        self.assertIn("Held", web_args["query"])
        self.assertIn("Alt", web_args["query"])

    async def test_binance_context_rides_in_the_same_batch(self):
        batch = AsyncMock(return_value=[[], [], {"social_hype_summary": "hype"}])
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            await self.client.gather_market_research("cid", "Will BTC rise?", "Yes", "No")
        requested = batch.await_args.args[0]
        self.assertEqual(
            [tool for tool, _ in requested],
            ["web_search", "x_posts_search", "binance_market_context"],
        )
        binance_args = requested[2][1]
        self.assertEqual(binance_args["query_tokens"], ["BTC"])
        self.assertEqual(binance_args["chain"], "solana")

    async def test_binance_context_is_cached_across_markets(self):
        batch = AsyncMock(
            return_value=[[{"title": "T"}], [{"title": "T"}], {"social_hype_summary": "hype"}]
        )
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            await self.client.gather_market_research("cid-a", "Will BTC rise?", "Yes", "No")
            await self.client.gather_market_research("cid-b", "Will Bitcoin rise?", "Yes", "No")
        # Both markets name BTC, so the second one skips the Binance request.
        self.assertEqual(batch.await_count, 2)
        self.assertEqual(
            [tool for tool, _ in batch.await_args_list[1].args[0]],
            ["web_search", "x_posts_search"],
        )

    async def test_binance_context_disabled_skips_its_request(self):
        batch = AsyncMock(return_value=[[{"title": "T"}], [{"title": "T"}]])
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "false"}),
            patch.object(self.client, "call_tools", new=batch),
        ):
            result = await self.client.gather_market_research("cid", "Will BTC rise?", "Yes", "No")
        self.assertEqual(
            [tool for tool, _ in batch.await_args.args[0]], ["web_search", "x_posts_search"]
        )
        self.assertEqual(result["binance_context"], {"formatted": ""})

    async def test_batch_flag_off_uses_per_call_transport(self):
        calls = AsyncMock(return_value=[{"title": "T"}])
        with (
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
            patch.dict(os.environ, {"RESEARCH_MCP_BATCH_ENABLED": "false"}),
            patch.object(self.client, "_call_tool", new=calls),
        ):
            await self.client.gather_market_research("cid", "Will BTC rise?", "Yes", "No")
        # Three tools → three per-call transports, as before the batching.
        self.assertEqual(calls.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in calls.await_args_list],
            ["web_search", "x_posts_search", "binance_market_context"],
        )


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
