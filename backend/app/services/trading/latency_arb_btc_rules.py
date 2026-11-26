"""BTC 15-minute rule guards for latency arbitrage (issue #153).

Pure functions. Polymarket resolves on Chainlink; Binance and Coinbase prices
are a proxy with basis risk. Nothing here verifies the oracle: ``oracle_verified``
stays False until a Chainlink source is read and compared per window.

``DEFAULT_BASIS_FLOOR`` is a configurable placeholder, not a measured value.
No Chainlink history is available in the repo to measure the real basis.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

DEFAULT_BASIS_FLOOR = 0.0005  # 5 bps of price: placeholder until measured
DEFAULT_MAX_REMAINING_SECONDS = 180.0
DEFAULT_MIN_Z = 2.0
BUCKETS_SECONDS = (300.0, 180.0, 60.0)  # last 5, 3 and 1 minutes


@dataclass(frozen=True)
class RuleDecision:
    enter: bool
    reason: str


def basis_risk_skip(distance: float, basis_floor: float = DEFAULT_BASIS_FLOOR) -> bool:
    """Skip when the move from the open is inside the proxy-vs-oracle basis."""
    if not math.isfinite(distance) or not math.isfinite(basis_floor) or basis_floor < 0:
        return True
    return abs(distance) < basis_floor


def late_window_decision(
    *,
    seconds_remaining: float,
    distance: float,
    sigma_per_window: float,
    fraction_remaining: float,
    max_remaining_seconds: float = DEFAULT_MAX_REMAINING_SECONDS,
    min_z: float = DEFAULT_MIN_Z,
    basis_floor: float = DEFAULT_BASIS_FLOOR,
) -> RuleDecision:
    """Enter only late in the window, far from the open relative to remaining vol."""
    values = (seconds_remaining, distance, sigma_per_window, fraction_remaining)
    if not all(math.isfinite(v) for v in values):
        return RuleDecision(False, "ABSTAIN_NON_FINITE_INPUT")
    if seconds_remaining <= 0 or fraction_remaining <= 0 or sigma_per_window <= 0:
        return RuleDecision(False, "ABSTAIN_WINDOW_CLOSED_OR_SIGMA_UNKNOWN")
    if seconds_remaining > max_remaining_seconds:
        return RuleDecision(False, "TOO_EARLY_IN_WINDOW")
    if basis_risk_skip(distance, basis_floor):
        return RuleDecision(False, "WITHIN_ORACLE_BASIS")
    z = abs(distance) / (sigma_per_window * math.sqrt(fraction_remaining))
    if z < min_z:
        return RuleDecision(False, "DISTANCE_NOT_LARGE_VS_SIGMA")
    return RuleDecision(True, "OK")


def bucket_report(
    candidates: Iterable[tuple[float, bool]],
) -> dict[str, dict[str, float | int | None]]:
    """Group resolved candidates by time left: (seconds_remaining, predicted_side_won).

    Reports counts and hit rate per bucket only. It does not pick a bucket and
    says nothing about after-fee profit; a bucket must be chosen on data before
    the frozen test, and that data does not exist in this repo yet.
    """
    rows: Sequence[tuple[float, bool]] = list(candidates)
    out: dict[str, dict[str, float | int | None]] = {}
    for limit in BUCKETS_SECONDS:
        hits = [won for secs, won in rows if 0 < secs <= limit]
        out[f"last_{int(limit // 60)}m"] = {
            "n": len(hits),
            "hit_rate": (sum(hits) / len(hits)) if hits else None,
        }
    return out
