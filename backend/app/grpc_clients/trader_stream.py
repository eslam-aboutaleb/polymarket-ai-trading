"""Client-side handling of the trader-analysis frame stream (issue #59).

``TraderStreamAssembler`` turns a possibly duplicated or reordered frame
sequence (after a reconnect replay) into an ordered, de-duplicated event
sequence, and refuses to present replayed frames as live generation.
"""

from __future__ import annotations

from typing import Any

TERMINAL_EVENTS = frozenset({"completion", "error"})
MAX_PENDING_FRAMES = 64


class StreamGapError(RuntimeError):
    """Raised when frames are missing and cannot be recovered."""


def frame_to_event(frame: Any) -> dict[str, Any]:
    """Convert a ``TraderAnalysisFrame`` message to a plain event dict."""
    replayed = bool(frame.replayed)
    return {
        "analysis_id": frame.analysis_id,
        "event_type": frame.event_type,
        "provider_call_id": frame.provider_call_id,
        "sequence": int(frame.sequence),
        "observed_at": frame.observed_at,
        "replayed": replayed,
        "text": frame.text,
        "end_state": frame.end_state,
        "error_code": frame.error_code,
        "error_message": frame.error_message,
        "metadata": dict(frame.metadata),
        "output_tokens": int(frame.output_tokens),
        "time_to_first_token_ms": int(frame.time_to_first_token_ms),
        "total_ms": int(frame.total_ms),
        # Replayed frames are never labelled as live model tokens, whatever the
        # server said.
        "stream_kind": "buffered_replay" if replayed else (frame.stream_kind or "status"),
    }


class TraderStreamAssembler:
    """Ordering and de-duplication by frame sequence."""

    def __init__(self) -> None:
        self.last_sequence = 0
        self.terminal: dict[str, Any] | None = None
        self.duplicates_dropped = 0
        self._pending: dict[int, dict[str, Any]] = {}

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Accept one event; return the events now ready, in order."""
        sequence = event["sequence"]
        if sequence <= self.last_sequence or sequence in self._pending:
            self.duplicates_dropped += 1
            return []
        if sequence > self.last_sequence + 1:
            if len(self._pending) >= MAX_PENDING_FRAMES:
                raise StreamGapError("too many out-of-order frames")
            self._pending[sequence] = event
            return []
        ready = [event]
        self.last_sequence = sequence
        while self.last_sequence + 1 in self._pending:
            nxt = self._pending.pop(self.last_sequence + 1)
            ready.append(nxt)
            self.last_sequence += 1
        for item in ready:
            if item["event_type"] in TERMINAL_EVENTS:
                self.terminal = item
        return ready

    def finish(self) -> None:
        """Call at end of stream: a gap that never filled is an error."""
        if self._pending:
            raise StreamGapError(
                f"missing frame {self.last_sequence + 1}; {len(self._pending)} frames held"
            )
