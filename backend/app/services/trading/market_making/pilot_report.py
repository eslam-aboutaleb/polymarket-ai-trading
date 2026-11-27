"""Maker pilot report (issue #156): live and modeled numbers are never merged.

The report is a pure aggregation over fill records the caller supplies. It
cannot run a pilot and places no orders. Every figure is grouped under its
``source`` label (``live`` or ``modeled``); a record with any other label is
rejected. Maker claims stay ``unverified`` unless the caller passes the result
of the promotion-gate out-of-sample check, which defaults to not passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SOURCES = ("live", "modeled")
ZERO = Decimal(0)


@dataclass(frozen=True)
class PilotFill:
    source: str  # "live" (venue-confirmed) or "modeled"
    shares: Decimal
    fee: Decimal  # absolute USDC fee paid on the fill
    pnl_after_fee: Decimal  # USDC, already net of fee
    markout_30s: Decimal | None  # per share; None when no frozen mark exists
    queue_assumed_ahead: Decimal | None  # modeled queue position at quote time
    queue_observed_ahead: Decimal | None  # venue-observed queue position, live only


def build_pilot_report(fills: list[PilotFill], *, promotion_gate_passed: bool = False) -> dict:
    groups: dict[str, list[PilotFill]] = {name: [] for name in SOURCES}
    for fill in fills:
        if fill.source not in groups:
            raise ValueError(f"unknown fill source: {fill.source!r}")
        groups[fill.source].append(fill)
    report: dict = {"sources": {}, "claim_status": "unverified"}
    for name, rows in groups.items():
        marks = [row.markout_30s for row in rows if row.markout_30s is not None]
        gaps = [
            row.queue_observed_ahead - row.queue_assumed_ahead
            for row in rows
            if row.queue_observed_ahead is not None and row.queue_assumed_ahead is not None
        ]
        report["sources"][name] = {
            "fills": len(rows),
            "shares": str(sum((r.shares for r in rows), ZERO)),
            "fees_usdc": str(sum((r.fee for r in rows), ZERO)),
            "pnl_after_fee_usdc": str(sum((r.pnl_after_fee for r in rows), ZERO)),
            "markout_30s_mean": str(sum(marks) / len(marks)) if marks else None,
            "markout_30s_samples": len(marks),
            "queue_observed_minus_assumed_mean": str(sum(gaps) / len(gaps)) if gaps else None,
        }
    live = report["sources"]["live"]["fills"]
    if live == 0:
        report["note"] = "No live fills: nothing here is evidence of real maker fills or queue."
    if promotion_gate_passed and live > 0:
        report["claim_status"] = "gate_passed"
    return report
