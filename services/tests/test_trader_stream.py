"""Genuine trader-analysis streaming over a real gRPC channel (issue #59).

The real servicer, ``TraderStreamer`` and ``TradingAnalysisChain`` run behind a
loopback ``grpc.aio`` server. Only the chat model is a deterministic fake whose
``astream`` yields deltas on demand, so the tests can prove frames reach the
client before generation finishes (no buffered replay, no pacing).
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("grpc_reflection")
pytest.importorskip("langchain_litellm")

SERVICE_ROOT = Path(__file__).resolve().parents[1] / "llm-chain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(SERVICE_ROOT.parent / "shared"))

import analysis_pb2  # noqa: E402
import analysis_pb2_grpc  # noqa: E402
import grpc  # noqa: E402
from src import analysis_chain, server  # noqa: E402
from src.config import Settings  # noqa: E402
from src.trader_stream import TraderStreamer, split_frames  # noqa: E402


def chunk(content, **extra):
    return SimpleNamespace(content=content, **extra)


class ScriptedLLM:
    """``astream`` yields the scripted items; callables are awaited first."""

    def __init__(self, script):
        self.script = script
        self.pulled = 0
        self.closed = False
        self.calls = 0

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(content="unary full text")

    def astream(self, messages):
        async def gen():
            try:
                for item in self.script:
                    if callable(item):
                        item = await item()
                    if isinstance(item, Exception):
                        raise item
                    self.pulled += 1
                    yield item
            finally:
                self.closed = True

        return gen()


class FakeSearch:
    def run(self, query):
        return f"result for {query}"


def make_settings(**overrides):
    return Settings(
        _env_file=None,
        gateway_base="https://gateway.example/v1",
        gateway_key="test",
        **overrides,
    )


class Harness:
    def __init__(self, llm, **settings):
        self.llm = llm
        self.settings = make_settings(**settings)
        self.servicer = server.AnalysisServicer.__new__(server.AnalysisServicer)
        self.servicer.settings = self.settings
        self.servicer.trader_streamer = TraderStreamer(self.settings)
        self.chain = analysis_chain.TradingAnalysisChain(llm_instance=llm)
        self.chain.search_tool = FakeSearch()
        self.requests_seen = []

    async def __aenter__(self):
        # Use the shipped prompts so the real trader prompt template is exercised.
        self._prompts = self.chain.prompts._prompts
        shipped = Path(__file__).resolve().parents[2] / "prompts" / "prompts.json"
        self.chain.prompts._prompts = json.loads(shipped.read_text())
        self._orig = server._get_chain_for_request

        def pick(request):
            self.requests_seen.append(request)
            return self.chain

        server._get_chain_for_request = pick
        self.server = grpc.aio.server()
        analysis_pb2_grpc.add_AnalysisServiceServicer_to_server(self.servicer, self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        await self.server.start()
        self.channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.stub = analysis_pb2_grpc.AnalysisServiceStub(self.channel)
        return self

    async def __aexit__(self, *exc):
        await self.channel.close()
        await self.server.stop(None)
        server._get_chain_for_request = self._orig
        self.chain.prompts._prompts = self._prompts

    def request(self, analysis_id="", resume=0, wallet="0xabc123456789"):
        return analysis_pb2.TraderStreamRequest(
            request=analysis_pb2.TraderAnalysisRequest(
                wallet_address=wallet,
                display_name="whale",
                user_id="7",
                llm_config=analysis_pb2.LLMConfig(provider=2, model="claude-x"),
            ),
            analysis_id=analysis_id,
            resume_after_sequence=resume,
        )

    async def collect(self, **kwargs):
        return [f async for f in self.stub.AnalyzeTraderStream(self.request(**kwargs), timeout=20)]


def run(coro):
    return asyncio.run(coro)


def text_of(frames):
    return "".join(f.text for f in frames if f.event_type == "content")


def test_frames_arrive_before_generation_finishes_and_are_typed_and_ordered():
    async def go():
        gate = asyncio.Event()

        async def wait_for_client():
            await asyncio.wait_for(gate.wait(), 10)
            return chunk("second ")

        llm = ScriptedLLM(
            [chunk("first "), wait_for_client, chunk("third", usage_metadata={"output_tokens": 9})]
        )
        async with Harness(llm) as h:
            frames = []
            started = time.perf_counter()
            async for frame in h.stub.AnalyzeTraderStream(h.request(), timeout=20):
                frames.append(frame)
                if frame.event_type == "content" and frame.text == "first ":
                    # The provider is still blocked: this frame cannot be a replay.
                    assert llm.pulled == 1
                    gate.set()
            elapsed = time.perf_counter() - started
        return frames, elapsed

    frames, elapsed = run(go())
    kinds = [f.event_type for f in frames]
    assert kinds == ["status", "status", "metadata", "content", "content", "content", "completion"]
    assert [f.sequence for f in frames] == list(range(1, 8))
    assert len({f.analysis_id for f in frames}) == 1 and frames[0].analysis_id
    assert not any(f.replayed for f in frames)
    assert {f.stream_kind for f in frames if f.event_type == "content"} == {"model_tokens"}
    assert text_of(frames) == "first second third"
    done = frames[-1]
    assert done.end_state == "completed"
    assert done.output_tokens == 9
    assert done.metadata["output_tokens_source"] == "provider"
    assert done.metadata["provider_streamed"] == "true"
    assert "result for polymarket trader" in done.metadata["research_context"]
    assert done.time_to_first_token_ms >= 1 and done.total_ms >= done.time_to_first_token_ms
    assert frames[2].metadata["provider"] == "anthropic"  # provider selection preserved
    assert elapsed < 5  # no artificial pacing


def test_unicode_split_bytes_lone_surrogate_and_nul_are_safe():
    emoji = "😀".encode()

    async def go():
        llm = ScriptedLLM(
            [
                chunk(emoji[:2]),
                chunk(emoji[2:]),
                chunk("a\ud800b\x00c"),
                chunk([{"text": "é"}, "ß"]),
            ]
        )
        async with Harness(llm) as h:
            return await h.collect()

    frames = run(go())
    assert text_of(frames) == "😀a?bcéß"
    assert frames[-1].event_type == "completion"


def test_large_output_is_split_and_output_limit_truncates():
    async def go():
        big = "x" * 50_000 + "é"
        async with Harness(ScriptedLLM([chunk(big)]), trader_stream_max_frame_chars=4000) as h:
            full = await h.collect()
        async with Harness(
            ScriptedLLM([chunk("y" * 10_000)]),
            trader_stream_max_frame_chars=4000,
            trader_stream_max_output_chars=6000,
        ) as h:
            capped = await h.collect()
        return big, full, capped

    big, full, capped = run(go())
    contents = [f for f in full if f.event_type == "content"]
    assert all(len(f.text) <= 4000 for f in contents)
    assert text_of(full) == big and full[-1].event_type == "completion"
    assert text_of(capped) == "y" * 6000
    assert capped[-1].end_state == "truncated" and capped[-1].error_code == "OUTPUT_LIMIT"


def test_provider_timeouts_are_explicit_and_keep_partial_content():
    async def stall():
        await asyncio.sleep(5)
        return chunk("never")

    async def go():
        async with Harness(
            ScriptedLLM([stall]), trader_stream_first_token_timeout_seconds=0.1
        ) as h:
            first = await h.collect()
        async with Harness(
            ScriptedLLM([chunk("partial "), stall]), trader_stream_idle_timeout_seconds=0.1
        ) as h:
            idle = await h.collect()
        return first, idle

    first, idle = run(go())
    assert first[-1].end_state == "timeout" and first[-1].error_code == "DEADLINE_EXCEEDED"
    assert text_of(first) == ""
    assert text_of(idle) == "partial " and idle[-1].error_code == "DEADLINE_EXCEEDED"
    assert not any(f.event_type == "completion" for f in idle)


def test_upstream_429_is_mapped_and_secrets_are_not_leaked():
    async def go():
        llm = ScriptedLLM([chunk("a"), Exception("429 Too Many Requests key=sk-secret-123")])
        async with Harness(llm) as h:
            return await h.collect()

    frames = run(go())
    err = frames[-1]
    assert err.event_type == "error" and err.error_code == "RATE_LIMITED"
    assert "sk-secret" not in err.error_message and "sk-secret" not in str(frames)


def test_client_cancellation_stops_generation_and_resume_reports_incomplete():
    async def go():
        async def endless():
            await asyncio.sleep(0.01)
            return chunk("tok ")

        llm = ScriptedLLM([chunk("one ")] + [endless] * 10_000)
        async with Harness(llm) as h:
            call = h.stub.AnalyzeTraderStream(h.request(), timeout=20)
            seen = []
            async for frame in call:
                seen.append(frame)
                if frame.event_type == "content":
                    break
            call.cancel()
            await asyncio.sleep(0.3)
            pulled = llm.pulled
            await asyncio.sleep(0.2)
            assert llm.pulled == pulled  # generation stopped with the client
            assert llm.closed  # provider stream was closed, no quota burned afterwards
            analysis_id = seen[0].analysis_id
            replay = await h.collect(analysis_id=analysis_id, resume=2)
        return seen, replay

    seen, replay = run(go())
    assert replay[0].replayed and replay[0].stream_kind == "buffered_replay"
    assert replay[0].sequence == 3
    assert replay[-1].error_code == "RESUME_INCOMPLETE" and replay[-1].end_state == "cancelled"


def test_reconnect_replays_without_duplicates_and_expired_id_is_reported():
    async def go():
        async with Harness(ScriptedLLM([chunk("alpha "), chunk("beta")])) as h:
            live = await h.collect()
            analysis_id = live[0].analysis_id
            resumed = await h.collect(analysis_id=analysis_id, resume=4)
            unknown = await h.collect(analysis_id="does-not-exist", resume=3)
            return live, resumed, unknown

    live, resumed, unknown = run(go())
    assert [f.sequence for f in resumed] == [f.sequence for f in live if f.sequence > 4]
    assert all(f.replayed and f.stream_kind == "buffered_replay" for f in resumed)
    assert [f.text for f in resumed if f.event_type == "content"] == ["beta"]
    assert unknown[0].error_code == "RESUME_EXPIRED"


def test_backpressure_slow_consumer_does_not_let_provider_run_ahead():
    async def go():
        llm = ScriptedLLM([chunk("z" * 3500) for _ in range(400)])
        async with Harness(llm, trader_stream_max_frame_chars=4000) as h:
            call = h.stub.AnalyzeTraderStream(h.request(), timeout=30)
            count = 0
            async for frame in call:
                count += frame.event_type == "content"
                if count == 3:
                    break
            await asyncio.sleep(0.5)  # consumer stalls
            pulled = llm.pulled
            call.cancel()
            return pulled

    assert run(go()) < 150  # bounded by the transport window, not the 400 available


def test_invalid_request_unary_compat_and_registry_bounds():
    async def go():
        async with Harness(ScriptedLLM([chunk("x")])) as h:
            bad = await h.collect(wallet="")
            unary = await h.chain.analyze_trader(wallet_address="0xabc")
            return bad, unary, h

    bad, unary, h = run(go())
    assert bad[0].error_code == "INVALID_ARGUMENT"
    assert unary["analysis"] == "unary full text"  # unary path keeps working, buffered
    streamer = TraderStreamer(make_settings(trader_stream_replay_max_streams=2))
    for key in "abc":
        streamer._records[key] = SimpleNamespace(created=time.monotonic())
    streamer._evict()
    assert list(streamer._records) == ["b", "c"]
    assert split_frames("", 5) == [""] and split_frames("abcdef", 4) == ["abcd", "ef"]
