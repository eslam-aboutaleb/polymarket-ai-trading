"""Edge decay and feasibility from executable quotes (issue #12).

Edge is measured at the quote obtainable on arrival, after the measured delay,
never at the trigger quote. A missed fill stays in the report as a miss. A
non-positive net edge, or no explicit canary approval, blocks live promotion.
This module reports; it does not claim the strategy is profitable.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

D = Decimal


@dataclass(frozen=True)
class EdgeDecayReport:
    """One decision. Prices are token prices in [0, 1]; ``None`` means unknown."""

    trace_id: str
    p_side: Decimal  # model probability of the traded token
    original_quote: Decimal | None  # best ask at the trigger
    arrival_quote: Decimal | None  # best ask obtainable after the latency
    fee_per_share: Decimal | None
    filled: bool

    @property
    def original_edge(self) -> Decimal | None:
        return None if self.original_quote is None else self.p_side - self.original_quote

    @property
    def missed_fill(self) -> bool:
        return not self.filled

    @property
    def net_edge(self) -> Decimal | None:
        """Edge at the arrival quote after fees; ``None`` for misses or unknowns."""
        if not self.filled or self.arrival_quote is None or self.fee_per_share is None:
            return None
        return self.p_side - self.arrival_quote - self.fee_per_share

    @property
    def decay(self) -> Decimal | None:
        if self.original_quote is None or self.arrival_quote is None:
            return None
        return self.arrival_quote - self.original_quote  # positive = quote moved against us

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "original_quote": _s(self.original_quote),
            "arrival_quote": _s(self.arrival_quote),
            "fee_per_share": _s(self.fee_per_share),
            "original_edge": _s(self.original_edge),
            "net_edge": _s(self.net_edge),
            "decay": _s(self.decay),
            "missed_fill": self.missed_fill,
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def feasibility(
    reports: Sequence[EdgeDecayReport], *, canary_approved: bool = False
) -> dict[str, Any]:
    fills = [r for r in reports if r.filled]
    priced = [r.net_edge for r in fills if r.net_edge is not None]
    decays = [r.decay for r in reports if r.decay is not None]
    unknown_cost = [r for r in fills if r.net_edge is None]
    mean_net = sum(priced, D(0)) / len(priced) if priced else None
    original = [r.original_edge for r in reports if r.original_edge is not None]
    positive_net = mean_net is not None and mean_net > 0 and not unknown_cost
    reasons: list[str] = []
    if mean_net is None:
        reasons.append("NO_PRICED_FILLS")
    elif mean_net <= 0:
        reasons.append("NET_EDGE_NOT_POSITIVE")
    if unknown_cost:
        reasons.append("FILLS_WITH_UNKNOWN_QUOTE_OR_FEE")
    if not canary_approved:
        reasons.append("CANARY_NOT_EXPLICITLY_APPROVED")
    return {
        "n": len(reports),
        "fills": len(fills),
        "missed_fills": len(reports) - len(fills),
        "miss_rate": None if not reports else (len(reports) - len(fills)) / len(reports),
        "mean_original_edge": _s(sum(original, D(0)) / len(original)) if original else None,
        "mean_net_edge_at_arrival": _s(mean_net),
        "mean_quote_decay": _s(sum(decays, D(0)) / len(decays)) if decays else None,
        "live_promotion_allowed": positive_net and canary_approved,
        "reasons": reasons,
        "note": "Measurement only. This does not show the strategy is or will be profitable.",
    }


class ReportStore:
    """Thread-safe bounded buffer of edge-decay reports."""

    def __init__(self, capacity: int = 5000) -> None:
        self._reports: deque[EdgeDecayReport] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, report: EdgeDecayReport) -> None:
        with self._lock:
            self._reports.append(report)

    def snapshot(self) -> list[EdgeDecayReport]:
        with self._lock:
            return list(self._reports)

    def clear(self) -> None:
        with self._lock:
            self._reports.clear()


REPORTS = ReportStore()
