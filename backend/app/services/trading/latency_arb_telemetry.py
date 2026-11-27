"""Per-stage latency traces for latency arbitrage (issue #12).

Stage stamps use a monotonic clock, so differences are valid on one host. Wall
clock time is kept only as an annotation with an explicit uncertainty bound and
is never subtracted across hosts without that bound. A trace whose stamps go
backwards is marked invalid and excluded from percentiles instead of repaired.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Order matters: each stage is measured from the previous stamped one.
STAGES = (
    "event_received",
    "signal_ready",
    "book_received",
    "quote_selected",
    "risk_approved",
    "signing_done",
    "submitted",
    "acknowledged",
    "filled",
)
SUBMIT_STAGES = STAGES[: STAGES.index("submitted") + 1]


@dataclass(frozen=True)
class LatencyTrace:
    """Monotonic stage stamps in seconds; ``None`` means the stage never happened."""

    trace_id: str
    stamps: dict[str, float]
    wall_clock_at: datetime | None = None
    wall_clock_uncertainty_ms: float | None = None  # None = offset unknown
    outcome: str = "unknown"  # filled|no_fill|partial|rejected|unknown
    reject_reason: str | None = None

    def ordered(self) -> list[tuple[str, float]]:
        return [(s, self.stamps[s]) for s in STAGES if s in self.stamps]

    def is_valid(self) -> bool:
        values = [v for _, v in self.ordered()]
        if any(not math.isfinite(v) for v in values) or len(values) < 2:
            return False
        if set(self.stamps) - set(STAGES):
            return False
        return all(b >= a for a, b in zip(values, values[1:], strict=False))

    def stage_ms(self) -> dict[str, float]:
        """Milliseconds spent reaching each stamped stage from the previous one."""
        ordered = self.ordered()
        return {
            name: (value - ordered[i][1]) * 1000.0 for i, (name, value) in enumerate(ordered[1:])
        }

    def total_ms(self) -> float | None:
        ordered = self.ordered()
        return None if len(ordered) < 2 else (ordered[-1][1] - ordered[0][1]) * 1000.0

    def components_sum_to_total(self, tolerance_ms: float = 1e-6) -> bool:
        """The stage durations must add up to the end-to-end time."""
        total = self.total_ms()
        return total is not None and abs(sum(self.stage_ms().values()) - total) <= tolerance_ms

    def submitted(self) -> bool:
        return "submitted" in self.stamps


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0-100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


class TelemetryStore:
    """Thread-safe bounded buffer of traces."""

    def __init__(self, capacity: int = 5000) -> None:
        self._traces: deque[LatencyTrace] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, trace: LatencyTrace) -> None:
        with self._lock:
            self._traces.append(trace)

    def snapshot(self) -> list[LatencyTrace]:
        with self._lock:
            return list(self._traces)

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()


def summarize(traces: Sequence[LatencyTrace]) -> dict[str, Any]:
    valid = [t for t in traces if t.is_valid()]
    per_stage: dict[str, list[float]] = {}
    for trace in valid:
        for stage, ms in trace.stage_ms().items():
            per_stage.setdefault(stage, []).append(ms)
    totals = [t.total_ms() for t in valid if t.total_ms() is not None]
    submit_to_ack = [
        (t.stamps["acknowledged"] - t.stamps["submitted"]) * 1000.0
        for t in valid
        if "acknowledged" in t.stamps and "submitted" in t.stamps
    ]

    def pcts(values: list[float]) -> dict[str, float | None]:
        return {f"p{q}": percentile(values, q) for q in (50, 95, 99)}

    outcomes: dict[str, int] = {}
    for trace in traces:
        outcomes[trace.outcome] = outcomes.get(trace.outcome, 0) + 1
    return {
        "trace_count": len(traces),
        "valid_trace_count": len(valid),
        "invalid_trace_count": len(traces) - len(valid),
        "stage_ms": {stage: {"n": len(v), **pcts(v)} for stage, v in per_stage.items()},
        "total_ms": {"n": len(totals), **pcts(totals)},
        "submit_to_ack_ms": {"n": len(submit_to_ack), **pcts(submit_to_ack)},
        "outcomes": outcomes,
        "dropped_before_submit": sum(1 for t in traces if not t.submitted()),
        "unknown_clock_offset": sum(1 for t in traces if t.wall_clock_uncertainty_ms is None),
        "coherent_components": all(t.components_sum_to_total() for t in valid),
    }


STORE = TelemetryStore()
