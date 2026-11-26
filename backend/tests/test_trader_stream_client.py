"""Client-side trader stream handling: ordering, de-duplication, reconnect (issue #59)."""

import asyncio

import grpc
import pytest

from app.grpc_clients import analysis_pb2
from app.grpc_clients.analysis_client import AnalysisClient
from app.grpc_clients.trader_stream import (
    StreamGapError,
    TraderStreamAssembler,
    frame_to_event,
)


def frame(sequence, event_type="content", text="", replayed=False, **extra):
    return analysis_pb2.TraderAnalysisFrame(
        analysis_id="a1",
        event_type=event_type,
        sequence=sequence,
        text=text,
        replayed=replayed,
        stream_kind="model_tokens",
        **extra,
    )


def events(*frames):
    return [frame_to_event(f) for f in frames]


def test_assembler_drops_duplicates_and_reorders_out_of_order_frames():
    assembler = TraderStreamAssembler()
    order = [1, 2, 4, 3, 3, 2, 5]
    out = []
    for seq in order:
        kind = "completion" if seq == 5 else "content"
        out += assembler.feed(frame_to_event(frame(seq, kind, text=str(seq))))
    assert [e["sequence"] for e in out] == [1, 2, 3, 4, 5]
    assert assembler.duplicates_dropped == 2
    assert assembler.terminal["event_type"] == "completion"
    assembler.finish()


def test_assembler_reports_an_unfilled_gap_instead_of_finishing():
    assembler = TraderStreamAssembler()
    assembler.feed(frame_to_event(frame(1)))
    assembler.feed(frame_to_event(frame(3)))
    with pytest.raises(StreamGapError):
        assembler.finish()


def test_replayed_frames_are_never_labelled_live_model_tokens():
    live = frame_to_event(frame(1, text="x"))
    replay = frame_to_event(frame(2, text="y", replayed=True))
    assert live["stream_kind"] == "model_tokens" and not live["replayed"]
    assert replay["stream_kind"] == "buffered_replay" and replay["replayed"]


class FakeCall:
    def __init__(self, items):
        self.items = items
        self.cancelled = False

    def __aiter__(self):
        async def gen():
            for item in self.items:
                if isinstance(item, Exception):
                    raise item
                yield item

        return gen()

    def cancel(self):
        self.cancelled = True


class FakeRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE


def client_with(calls):
    seen = []

    class Stub:
        def AnalyzeTraderStream(self, request, timeout=None):
            seen.append((request.analysis_id, request.resume_after_sequence))
            return calls.pop(0)

    client = AnalysisClient.__new__(AnalysisClient)

    async def get_stub():
        return Stub()

    client._get_stub = get_stub
    return client, seen


def test_client_reconnects_with_resume_point_and_deduplicates_replay():
    first = FakeCall([frame(1, "status"), frame(2, text="a"), frame(3, text="b"), FakeRpcError()])
    second = FakeCall(
        [
            frame(3, text="b", replayed=True),  # overlap: must be dropped
            frame(4, text="c", replayed=True),
            frame(5, "completion", end_state="completed"),
        ]
    )
    client, seen = client_with([first, second])

    async def go():
        return [e async for e in client.analyze_trader_events(wallet_address="0xabc")]

    out = asyncio.run(go())
    assert [e["sequence"] for e in out] == [1, 2, 3, 4, 5]
    assert [e["text"] for e in out if e["event_type"] == "content"] == ["a", "b", "c"]
    assert out[3]["replayed"] and out[3]["stream_kind"] == "buffered_replay"
    assert not out[1]["replayed"]
    assert seen[0][1] == 0 and seen[1][1] == 3 and seen[0][0] == seen[1][0]
    assert first.cancelled and second.cancelled


def test_client_gives_up_after_bounded_reconnects_and_never_fakes_completion():
    calls = [FakeCall([frame(1, "status"), FakeRpcError()]) for _ in range(3)]
    client, _ = client_with(calls)

    async def go():
        return [
            e async for e in client.analyze_trader_events(wallet_address="0xabc", max_reconnects=2)
        ]

    with pytest.raises(RuntimeError, match="trader stream failed"):
        asyncio.run(go())


def test_stream_ending_without_terminal_event_is_an_error():
    calls = [FakeCall([frame(1, "status"), frame(2, text="x")]) for _ in range(2)]
    client, _ = client_with(calls)

    async def go():
        return [
            e async for e in client.analyze_trader_events(wallet_address="0xabc", max_reconnects=1)
        ]

    with pytest.raises(RuntimeError):
        asyncio.run(go())
