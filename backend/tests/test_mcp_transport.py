"""Tests for the batched research-MCP transport.

The transport speaks newline-delimited JSON to the research server: one
``{"tool": ..., "args": ...}`` request line per tool call and exactly one
response frame back per request line, in request order. A whole batch is served
by a single subprocess and terminated with an explicit ``__shutdown__`` frame.

Two levels are covered:

* mocked transport (frame ordering, padding, timeouts, partial failure,
  delegation from :meth:`ResearchMCPClient._call_tool`),
* a real stdio round-trip against a scripted stand-in server, which is what
  proves one process really does answer N requests and exits cleanly.
"""

import asyncio
import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "shared"))

from polymarket_mcp import ResearchMCPClient  # noqa: E402

# The server path and the subprocess double live in test_shared_mcp_client so
# the transport tests keep exactly one copy of each.
from test_shared_mcp_client import SERVER_PATH, make_proc  # noqa: E402

# A stand-in research server: echoes {"tool": <name>} for every request line,
# replies to __shutdown__ and then stops, and exits cleanly on EOF too.
_FAKE_SERVER = """\
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    payload = json.loads(line)
    if payload.get("tool") == "__shutdown__":
        sys.stdout.write(json.dumps({"ok": True, "result": {"shutdown": True}}) + "\\n")
        sys.stdout.flush()
        break
    result = {"tool": payload.get("tool")}
    if str(payload.get("tool", "")).startswith("binance"):
        # Enough real data for the caller to consider the context cacheable.
        result["social_hype_summary"] = "hype"
    sys.stdout.write(json.dumps({"ok": True, "result": result}) + "\\n")
    sys.stdout.flush()
"""


def _frame(tool: str, result) -> str:
    return json.dumps({"ok": True, "result": result}) + "\n"


class CallToolsMockedTransportTests(unittest.IsolatedAsyncioTestCase):
    """Frame handling with the subprocess stubbed out."""

    def setUp(self):
        self.client = ResearchMCPClient()
        self.client.server_path = SERVER_PATH

    async def _run(self, stdout, requests=None, returncode=0):
        requests = requests or [("t0", {}), ("t1", {}), ("t2", {})]
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=make_proc(stdout, returncode)),
        ):
            return await self.client.call_tools(requests)

    async def test_returns_one_result_per_request_in_order(self):
        stdout = (_frame("t0", {"n": 0}) + _frame("t1", {"n": 1}) + _frame("t2", {"n": 2})).encode()
        results = await self._run(stdout)
        self.assertEqual(results, [{"n": 0}, {"n": 1}, {"n": 2}])

    async def test_sends_one_request_line_per_tool_plus_shutdown(self):
        proc = make_proc(b"")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            await self.client.call_tools([("a", {"x": 1}), ("b", {"y": 2})])
        payload = proc.communicate.await_args.args[0].decode()
        frames = [json.loads(line) for line in payload.splitlines()]
        self.assertEqual(frames[0], {"tool": "a", "args": {"x": 1}})
        self.assertEqual(frames[1], {"tool": "b", "args": {"y": 2}})
        self.assertEqual(frames[2], {"tool": "__shutdown__", "args": {}})

    async def test_skips_non_json_noise_lines(self):
        stdout = (
            b"a stray import warning\n"
            + _frame("t0", {"n": 0}).encode()
            + b"\n"
            + _frame("t1", {"n": 1}).encode()
            + _frame("t2", {"n": 2}).encode()
        )
        results = await self._run(stdout)
        self.assertEqual(results, [{"n": 0}, {"n": 1}, {"n": 2}])

    async def test_partial_failure_is_isolated_to_the_failing_request(self):
        # One request answers with an error payload; its siblings still return.
        stdout = (
            _frame("t0", {"n": 0}) + '{"ok": false, "error": "nope"}\n' + _frame("t2", {"n": 2})
        ).encode()
        results = await self._run(stdout)
        self.assertEqual(results, [{"n": 0}, [], {"n": 2}])

    async def test_missing_frames_are_padded_with_empty(self):
        # The server died after answering two of three requests.
        stdout = (_frame("t0", {"n": 0}) + _frame("t1", {"n": 1})).encode()
        results = await self._run(stdout)
        self.assertEqual(results, [{"n": 0}, {"n": 1}, []])

    async def test_extra_frames_are_ignored(self):
        stdout = "".join(_frame(f"t{i}", {"n": i}) for i in range(5)).encode()
        results = await self._run(stdout, requests=[("t0", {}), ("t1", {})])
        self.assertEqual(results, [{"n": 0}, {"n": 1}])

    async def test_non_zero_exit_yields_empty_for_every_request(self):
        results = await self._run(_frame("t0", {"n": 0}).encode(), returncode=1)
        self.assertEqual(results, [[], [], []])

    async def test_timeout_yields_the_timeout_payload_and_kills_the_process(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            results = await self.client.call_tools([("t0", {}), ("t1", {})])
        self.assertEqual(
            results,
            [
                {"ok": False, "error": "research timed out"},
                {"ok": False, "error": "research timed out"},
            ],
        )
        proc.kill.assert_called_once()

    async def test_timeout_budget_scales_with_batch_size(self):
        # One wall-clock budget covers the whole batch (60 s per request), so
        # a 3-call batch may take 180 s — one hung tool spends the lot.
        wait_for = AsyncMock(return_value=(b"", b""))
        proc = MagicMock()
        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", new=wait_for),
        ):
            await self.client.call_tools([("t0", {}), ("t1", {}), ("t2", {})])
        self.assertEqual(wait_for.await_args.kwargs["timeout"], 180.0)

    async def test_subprocess_failure_yields_empty_for_every_request(self):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("spawn failed")),
        ):
            results = await self.client.call_tools([("t0", {}), ("t1", {})])
        self.assertEqual(results, [[], []])

    async def test_child_is_killed_when_communicate_raises(self):
        # A non-timeout failure must not abandon the process: it would still
        # hold the stdio pipes open for the rest of the service's lifetime.
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.communicate = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            results = await self.client.call_tools([("t0", {})])
        self.assertEqual(results, [[]])
        proc.kill.assert_called_once()

    async def test_child_is_killed_when_the_batch_is_cancelled(self):
        # CancelledError is a BaseException, so it slips past the generic
        # "Failure" handler — the finally clause is the only thing reaping it.
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError())
        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.client.call_tools([("t0", {})])
        proc.kill.assert_called_once()

    async def test_child_is_not_killed_after_a_clean_exit(self):
        proc = make_proc(_frame("t0", {"n": 0}).encode())
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            await self.client.call_tools([("t0", {})])
        proc.kill.assert_not_called()

    async def test_no_server_path_short_circuits(self):
        self.client.server_path = ""
        self.assertEqual(await self.client.call_tools([("t0", {})]), [[]])

    async def test_empty_request_list_returns_empty(self):
        self.assertEqual(await self.client.call_tools([]), [])


class CallToolDelegationTests(unittest.IsolatedAsyncioTestCase):
    """``_call_tool`` is the batched transport with a single request."""

    def setUp(self):
        self.client = ResearchMCPClient()
        self.client.server_path = SERVER_PATH

    async def test_delegates_through_call_tools(self):
        batch = AsyncMock(return_value=[{"a": 1}])
        with patch.object(self.client, "call_tools", new=batch):
            result = await self.client._call_tool("web_search", {"query": "x"})
        self.assertEqual(result, {"a": 1})
        self.assertEqual(batch.await_args.args[0], [("web_search", {"query": "x"})])

    async def test_single_request_is_decoded_like_before(self):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=make_proc(b'{"ok": true, "result": {"a": 1}}\n')),
        ):
            self.assertEqual(await self.client._call_tool("web_search", {}), {"a": 1})

    async def test_tolerates_leading_noise_like_before(self):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=make_proc(b'noise\n{"ok": true, "result": {"f": 1}}\n')),
        ):
            self.assertEqual(await self.client._call_tool("web_search", {}), {"f": 1})

    async def test_malformed_json_yields_empty(self):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=make_proc(b"not json at all\n")),
        ):
            self.assertEqual(await self.client._call_tool("web_search", {}), [])


class DecodeResponseFrameTests(unittest.TestCase):
    """The per-frame decoding rules that back the transport."""

    def test_error_frame_yields_empty(self):
        self.assertEqual(ResearchMCPClient._decode_response({"ok": False}), [])

    def test_object_result_is_returned(self):
        payload = {"ok": True, "result": {"a": 1}}
        self.assertEqual(ResearchMCPClient._decode_response(payload), {"a": 1})

    def test_list_result_filters_non_dict_rows(self):
        payload = {"ok": True, "result": [{"a": 1}, "junk", 5]}
        self.assertEqual(ResearchMCPClient._decode_response(payload), [{"a": 1}])

    def test_null_and_scalar_results_yield_empty(self):
        self.assertEqual(ResearchMCPClient._decode_response({"ok": True, "result": None}), [])
        self.assertEqual(ResearchMCPClient._decode_response({"ok": True, "result": 42}), [])

    def test_non_dict_frames_are_skipped_when_decoding_a_stream(self):
        raw = "42\n" + _frame("t0", {"n": 0})
        self.assertEqual(ResearchMCPClient._decode_response_frames(raw, 1), [{"n": 0}])

    def test_missing_frames_are_padded(self):
        self.assertEqual(ResearchMCPClient._decode_response_frames("", 2), [[], []])


class RealSubprocessTransportTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end round-trip against a real scripted stdio server."""

    def setUp(self):
        ResearchMCPClient._cache.clear()
        ResearchMCPClient._binance_cache.clear()
        self.client = ResearchMCPClient()

    def _write_fake_server(self) -> str:
        import tempfile

        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = Path(tmp) / "fake_research_server.py"
        path.write_text(_FAKE_SERVER)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    async def test_batch_is_served_by_one_process_and_exits_cleanly(self):
        self.client.server_path = self._write_fake_server()
        spawns = []
        original = asyncio.create_subprocess_exec

        async def counting(*args, **kwargs):
            spawns.append(args)
            return await original(*args, **kwargs)

        with patch("asyncio.create_subprocess_exec", new=counting):
            results = await self.client.call_tools(
                [("alpha", {"i": 1}), ("beta", {"i": 2}), ("gamma", {"i": 3})]
            )
        self.assertEqual(results, [{"tool": "alpha"}, {"tool": "beta"}, {"tool": "gamma"}])
        # One subprocess for the whole batch.
        self.assertEqual(len(spawns), 1)

    async def test_gather_market_research_spawns_one_subprocess(self):
        self.client.server_path = self._write_fake_server()
        spawns = []
        original = asyncio.create_subprocess_exec

        async def counting(*args, **kwargs):
            spawns.append(args)
            return await original(*args, **kwargs)

        with (
            patch("asyncio.create_subprocess_exec", new=counting),
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
        ):
            result = await self.client.gather_market_research("cid", "Will BTC rise?", "Yes", "No")
        for key in ("web_results", "x_results", "binance_context"):
            self.assertIn(key, result)
        # Was 3 subprocesses (web, X, Binance) before the batch transport.
        self.assertEqual(len(spawns), 1)

    async def test_gather_market_research_spawns_three_when_batching_is_disabled(self):
        self.client.server_path = self._write_fake_server()
        spawns = []
        original = asyncio.create_subprocess_exec

        async def counting(*args, **kwargs):
            spawns.append(args)
            return await original(*args, **kwargs)

        with (
            patch("asyncio.create_subprocess_exec", new=counting),
            patch.dict(
                os.environ,
                {"BINANCE_SKILLS_ENABLED": "true", "RESEARCH_MCP_BATCH_ENABLED": "false"},
            ),
        ):
            await self.client.gather_market_research("cid", "Will BTC rise?", "Yes", "No")
        # The kill-switch falls back to one process per tool call.
        self.assertEqual(len(spawns), 3)

    async def test_gather_binance_context_is_cached_across_calls(self):
        self.client.server_path = self._write_fake_server()
        spawns = []
        original = asyncio.create_subprocess_exec

        async def counting(*args, **kwargs):
            spawns.append(args)
            return await original(*args, **kwargs)

        with (
            patch("asyncio.create_subprocess_exec", new=counting),
            patch.dict(os.environ, {"BINANCE_SKILLS_ENABLED": "true"}),
        ):
            first = await self.client._gather_binance_context("Will BTC rise?")
            second = await self.client._gather_binance_context("Will Bitcoin rally?")
        self.assertEqual(first, second)
        # Both titles name BTC, so the second call is served from the cache.
        self.assertEqual(len(spawns), 1)


class ResearchServerShutdownTests(unittest.TestCase):
    """The server's framing contract and its explicit shutdown frame."""

    @classmethod
    def setUpClass(cls):
        import types

        # ``ddgs`` is an optional runtime dep that the backend test venv does
        # not install; the shutdown path never touches it.
        if "ddgs" not in sys.modules:
            sys.modules["ddgs"] = types.ModuleType("ddgs")
            sys.modules["ddgs"].DDGS = MagicMock()
        import importlib.util

        server_file = (
            Path(__file__).resolve().parents[2] / "services" / "mcps" / "research" / "server.py"
        )
        spec = importlib.util.spec_from_file_location("research_server_under_test", server_file)
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def _run_main(self, lines: list[str]) -> tuple[int, list[dict]]:
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", stdout):
            code = self.server.main()
        frames = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        return code, frames

    def test_one_response_frame_per_request_line_in_order(self):
        code, frames = self._run_main(
            [
                json.dumps({"tool": "no-such-tool-one", "args": {}}),
                json.dumps({"tool": "no-such-tool-two", "args": {}}),
                json.dumps({"tool": "__shutdown__", "args": {}}),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(frames), 3)
        self.assertIn("no-such-tool-one", frames[0]["error"])
        self.assertIn("no-such-tool-two", frames[1]["error"])
        self.assertEqual(frames[2], {"ok": True, "result": {"shutdown": True}})

    def test_shutdown_alone_acknowledges_and_stops(self):
        code, frames = self._run_main([json.dumps({"tool": "__shutdown__", "args": {}})])
        self.assertEqual(code, 0)
        self.assertEqual(frames, [{"ok": True, "result": {"shutdown": True}}])

    def test_eof_also_exits_cleanly(self):
        code, frames = self._run_main([json.dumps({"tool": "no-such-tool", "args": {}})])
        self.assertEqual(code, 0)
        self.assertEqual(len(frames), 1)


if __name__ == "__main__":
    unittest.main()
