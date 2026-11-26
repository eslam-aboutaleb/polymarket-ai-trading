"""Provider-backed streaming for trader analysis (issue #59).

``TraderStreamer.stream`` turns a model's async token stream into typed
frames: ``status``, ``metadata``, ``content``, ``completion`` and ``error``.
Every frame has a monotonic sequence number, a ``provider_call_id`` and a
``replayed`` flag. Content frames are only ever produced from deltas the
provider actually yielded; nothing is paced, split for effect or synthesised.
Frames served from the replay buffer after a reconnect are flagged
``replayed=True`` and ``stream_kind="buffered_replay"`` so a UI can never
present them as live generation.

Backpressure: the provider is pulled one delta at a time and a delta is only
requested again after the previous frame was consumed by the gRPC writer, so a
slow client slows the provider read instead of growing a buffer. The replay
buffer itself is bounded (streams, frames and TTL).

Cancellation: when the client goes away the handler is cancelled; the
provider stream is closed, the record is marked ``cancelled`` and no further
tokens are generated (quota is not spent on an abandoned stream).
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

TERMINAL_EVENTS = frozenset({"completion", "error"})


@dataclass(frozen=True)
class Frame:
    analysis_id: str
    event_type: str
    sequence: int
    observed_at: str
    provider_call_id: str = ""
    replayed: bool = False
    text: str = ""
    end_state: str = ""
    error_code: str = ""
    error_message: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    output_tokens: int = 0
    time_to_first_token_ms: int = 0
    total_ms: int = 0
    stream_kind: str = "status"


@dataclass
class StreamRecord:
    frames: list[Frame] = field(default_factory=list)
    state: str = "running"  # running | finished | cancelled
    created: float = 0.0
    replayable: bool = True
    next_sequence: int = 1


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def clean_text(text: str) -> str:
    """Make a delta safe for protobuf: no lone surrogates, no NULs."""
    return text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")


def delta_text(content: Any, decoder: codecs.IncrementalDecoder) -> str:
    """Normalise a provider delta (str, bytes or content-part list) to text.

    Bytes are decoded incrementally so a multibyte character split across two
    deltas is reassembled instead of becoming two replacement characters.
    """
    if content is None:
        return ""
    if isinstance(content, bytes | bytearray):
        return decoder.decode(bytes(content))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            delta_text(part.get("text", "") if isinstance(part, dict) else part, decoder)
            for part in content
        )
    return ""


def split_frames(text: str, size: int) -> list[str]:
    """Split on code point boundaries so large deltas never break a character."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def classify_error(exc: BaseException) -> tuple[str, str]:
    """Map a provider failure to a sanitised (code, message); never echo exc text."""
    name = type(exc).__name__
    text = f"{name} {exc}".lower()
    if isinstance(exc, TimeoutError | asyncio.TimeoutError) or "timeout" in text:
        return "DEADLINE_EXCEEDED", "Provider timed out"
    if "429" in text or "rate limit" in text or "ratelimit" in text or "quota" in text:
        return "RATE_LIMITED", "Provider rate limit reached"
    return "PROVIDER_ERROR", f"Provider error ({name})"


class TraderStreamer:
    """Streams one trader analysis and keeps a bounded replay buffer."""

    def __init__(self, settings: Any, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self._clock = clock
        self._records: OrderedDict[str, StreamRecord] = OrderedDict()

    # ── replay buffer ────────────────────────────────────────────────
    def _evict(self) -> None:
        ttl = self.settings.trader_stream_replay_ttl_seconds
        now = self._clock()
        for key in [k for k, r in self._records.items() if now - r.created > ttl]:
            del self._records[key]
        while len(self._records) > self.settings.trader_stream_replay_max_streams:
            self._records.popitem(last=False)

    def record(self, analysis_id: str) -> StreamRecord | None:
        self._evict()
        return self._records.get(analysis_id)

    def _frame(self, rec: StreamRecord, analysis_id: str, event: str, **fields: Any) -> Frame:
        frame = Frame(
            analysis_id=analysis_id,
            event_type=event,
            sequence=rec.next_sequence,
            observed_at=now_iso(),
            **fields,
        )
        rec.next_sequence += 1
        if len(rec.frames) < self.settings.trader_stream_replay_max_frames:
            rec.frames.append(frame)
        else:
            # Over the replay cap: keep generating live but stop retaining frames.
            rec.replayable = False
        return frame

    # ── replay ───────────────────────────────────────────────────────
    async def replay(self, analysis_id: str, resume_after: int) -> AsyncIterator[Frame]:
        """Serve buffered frames after ``resume_after`` as replayed, never live."""
        rec = self.record(analysis_id)
        seq_base = resume_after
        if rec is None:
            yield Frame(
                analysis_id=analysis_id,
                event_type="error",
                sequence=resume_after + 1,
                observed_at=now_iso(),
                replayed=True,
                end_state="failed",
                error_code="RESUME_EXPIRED",
                error_message="Stream is unknown or expired; restart without analysis_id",
                stream_kind="buffered_replay",
            )
            return
        frames = list(rec.frames)
        for frame in frames:
            if frame.sequence > seq_base:
                yield replace(frame, replayed=True, stream_kind="buffered_replay")
        terminal = bool(frames) and frames[-1].event_type in TERMINAL_EVENTS
        if not terminal or not rec.replayable:
            yield Frame(
                analysis_id=analysis_id,
                event_type="error",
                sequence=(frames[-1].sequence + 1) if frames else 1,
                observed_at=now_iso(),
                replayed=True,
                end_state="cancelled" if rec.state == "cancelled" else "failed",
                error_code="RESUME_INCOMPLETE",
                error_message="Original stream did not finish; restart without analysis_id",
                stream_kind="buffered_replay",
            )

    # ── live generation ──────────────────────────────────────────────
    async def stream(
        self,
        *,
        llm: Any,
        messages: list,
        research: Callable[[], str] | None = None,
        analysis_id: str = "",
        provider: dict[str, str] | None = None,
        time_remaining: Callable[[], float | None] | None = None,
    ) -> AsyncIterator[Frame]:
        s = self.settings
        analysis_id = analysis_id or uuid.uuid4().hex
        self._evict()
        rec = StreamRecord(created=self._clock())
        self._records[analysis_id] = rec
        provider_call_id = uuid.uuid4().hex
        started = self._clock()
        deadline = s.trader_stream_deadline_seconds
        remaining = time_remaining() if time_remaining else None
        if remaining is not None:
            deadline = min(deadline, max(remaining, 0.0))

        def left() -> float:
            return deadline - (self._clock() - started)

        def emit(event: str, **fields: Any) -> Frame:
            fields.setdefault("provider_call_id", provider_call_id)
            return self._frame(rec, analysis_id, event, **fields)

        iterator = None
        try:
            yield emit("status", metadata={"phase": "started"}, stream_kind="status")
            research_context = ""
            if research is not None:
                yield emit("status", metadata={"phase": "researching"}, stream_kind="status")
                research_context = await asyncio.wait_for(
                    asyncio.to_thread(research), timeout=max(left(), 0.001)
                )
            yield emit(
                "metadata",
                metadata={**(provider or {}), "stream_kind": "model_tokens"},
                stream_kind="status",
            )

            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            first_token_ms = 0
            chars = 0
            chunk_count = 0
            output_tokens = 0
            call_started = self._clock()
            iterator = llm.astream(messages).__aiter__()
            while True:
                timeout = s.trader_stream_idle_timeout_seconds
                if chunk_count == 0:
                    timeout = s.trader_stream_first_token_timeout_seconds
                timeout = min(timeout, left())
                if timeout <= 0:
                    raise TimeoutError("stream deadline")
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    break
                usage = getattr(chunk, "usage_metadata", None)
                if isinstance(usage, dict) and usage.get("output_tokens"):
                    output_tokens = int(usage["output_tokens"])
                text = clean_text(delta_text(getattr(chunk, "content", chunk), decoder))
                if not text:
                    continue
                chunk_count += 1
                if not first_token_ms:
                    first_token_ms = max(int((self._clock() - call_started) * 1000), 1)
                for part in split_frames(text, s.trader_stream_max_frame_chars):
                    if chars + len(part) > s.trader_stream_max_output_chars:
                        part = part[: max(s.trader_stream_max_output_chars - chars, 0)]
                        if part:
                            yield emit("content", text=part, stream_kind="model_tokens")
                        yield emit(
                            "error",
                            end_state="truncated",
                            error_code="OUTPUT_LIMIT",
                            error_message="Output limit reached",
                            total_ms=max(int((self._clock() - started) * 1000), first_token_ms),
                            time_to_first_token_ms=first_token_ms,
                            stream_kind="model_tokens",
                        )
                        rec.state = "finished"
                        await _aclose(iterator)
                        return
                    chars += len(part)
                    yield emit("content", text=part, stream_kind="model_tokens")
            tail = clean_text(decoder.decode(b"", final=True))
            if tail:
                yield emit("content", text=tail, stream_kind="model_tokens")
            yield emit(
                "completion",
                end_state="completed",
                output_tokens=output_tokens,
                time_to_first_token_ms=first_token_ms,
                total_ms=max(int((self._clock() - started) * 1000), first_token_ms),
                stream_kind="model_tokens",
                metadata={
                    "research_context": research_context,
                    "chunk_count": str(chunk_count),
                    "provider_streamed": str(chunk_count > 1).lower(),
                    "output_chars": str(chars),
                    "output_tokens_source": "provider" if output_tokens else "unavailable",
                },
            )
            rec.state = "finished"
        except asyncio.CancelledError:
            rec.state = "cancelled"
            if iterator is not None:
                await _aclose(iterator)
            raise
        except GeneratorExit:
            rec.state = "cancelled"
            if iterator is not None:
                await _aclose(iterator)
            raise
        except Exception as exc:
            code, message = classify_error(exc)
            logger.warning("Trader stream %s failed: %s", analysis_id, type(exc).__name__)
            if iterator is not None:
                await _aclose(iterator)
            rec.state = "finished"
            yield emit(
                "error",
                end_state="timeout" if code == "DEADLINE_EXCEEDED" else "failed",
                error_code=code,
                error_message=message,
                total_ms=int((self._clock() - started) * 1000),
                stream_kind="model_tokens",
            )


async def _aclose(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception as exc:  # closing is best effort
            logger.debug("stream close failed: %s", type(exc).__name__)
