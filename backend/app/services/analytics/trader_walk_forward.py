"""Walk-forward check: do top-scored traders' followers beat bottom-scored ones? (#158)

Scores are computed only from positions closed strictly before each cutoff.
Follower returns come only from leader trades first seen at or after that
cutoff (and before the next one), simulated at the follower's delay on captured
books and net of fees. Nothing scored on the same data it is judged on.

The verdict is deliberately conservative. ``top_beats_bottom`` needs a
bootstrap interval (resampling traders, not trades) for top minus bottom that
sits wholly above zero after costs. Too few traders or trades is reported as
``insufficient_evidence``. Otherwise the report says plainly that no
advantage was shown.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.analytics.follower_lag import (
    LagSettings,
    LeaderTrade,
    simulate_follower_fill,
)
from app.services.analytics.replay.follower import Book
from app.services.analytics.trader_scoring import ResolvedPosition, score_resolved_positions

MODEL_VERSION = "trader-walk-forward-v1"
MIN_TRADERS_PER_BUCKET = 3
MIN_TEST_TRADES = 30


class TraderHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    wallet: str = Field(pattern=r"^0x[a-fA-F0-9]{40}$")
    positions: list[ResolvedPosition] = Field(max_length=5000)
    leader_trades: list[LeaderTrade] = Field(max_length=5000)


def _pooled(rows: list[tuple[Decimal, Decimal]]) -> float | None:
    cost = sum((c for c, _ in rows), Decimal(0))
    if cost <= 0:
        return None
    return float(sum((p for _, p in rows), Decimal(0)) / cost)


def _bucket_return(per_trader: list[list[tuple[Decimal, Decimal]]]) -> float | None:
    return _pooled([row for trader in per_trader for row in trader])


def bootstrap_difference(
    top: list[list[tuple[Decimal, Decimal]]],
    bottom: list[list[tuple[Decimal, Decimal]]],
    *,
    seed: int,
    resamples: int,
) -> tuple[float, float] | None:
    """95% interval for pooled top return minus pooled bottom return, by trader."""
    rng = random.Random(seed)  # noqa: S311 - deterministic statistical resampling
    diffs = []
    for _ in range(resamples):
        a = _bucket_return([rng.choice(top) for _ in top])
        b = _bucket_return([rng.choice(bottom) for _ in bottom])
        if a is not None and b is not None:
            diffs.append(a - b)
    if len(diffs) < resamples // 2:
        return None
    diffs.sort()
    return diffs[int(0.025 * len(diffs))], diffs[min(len(diffs) - 1, int(0.975 * len(diffs)))]


def walk_forward(
    histories: list[TraderHistory],
    books: list[Book],
    *,
    cutoffs: list[datetime],
    end_at: datetime,
    delay_seconds: int = 60,
    settings: LagSettings | None = None,
    quantile: float = 0.25,
    seed: int = 7,
    resamples: int = 2000,
) -> dict[str, Any]:
    settings = settings or LagSettings()
    ordered = sorted(cutoffs)
    if not ordered or any(a >= b for a, b in zip(ordered, ordered[1:], strict=False)):
        raise ValueError("cutoffs must be strictly increasing and non-empty")
    if end_at <= ordered[-1]:
        raise ValueError("end_at must be after the last cutoff")
    if not 0 < quantile <= 0.5:
        raise ValueError("quantile must be in (0, 0.5]")
    bounds = [*ordered, end_at]
    top_rows: list[list[tuple[Decimal, Decimal]]] = []
    bottom_rows: list[list[tuple[Decimal, Decimal]]] = []
    folds = []
    for index, cutoff in enumerate(ordered):
        upper = bounds[index + 1]
        scored = []
        for history in histories:
            train = [p for p in history.positions if p.closed_at < cutoff]
            result = score_resolved_positions(train)
            if result["status"] == "scored":
                scored.append((result["score"], history.wallet.lower(), history))
        scored.sort(key=lambda item: (-item[0], item[1]))
        size = max(1, math.ceil(len(scored) * quantile)) if scored else 0
        bucket_top = scored[:size]
        bucket_bottom = scored[-size:] if size else []
        overlap = {w for _, w, _ in bucket_top} & {w for _, w, _ in bucket_bottom}
        fold = {
            "cutoff": cutoff.isoformat(),
            "test_until": upper.isoformat(),
            "scored_traders": len(scored),
            "top": [w for _, w, _ in bucket_top],
            "bottom": [w for _, w, _ in bucket_bottom],
        }
        if overlap or len(scored) < 2 * MIN_TRADERS_PER_BUCKET:
            fold["skipped"] = "too_few_scored_traders_for_disjoint_buckets"
            folds.append(fold)
            continue
        for label, bucket, sink in (
            ("top", bucket_top, top_rows),
            ("bottom", bucket_bottom, bottom_rows),
        ):
            for _, _, history in bucket:
                rows = []
                for trade in history.leader_trades:
                    if not cutoff <= trade.first_seen_at < upper:
                        continue
                    fill = simulate_follower_fill(trade, books, delay_seconds, settings)
                    if fill["status"] != "unfilled" and "follower_pnl" in fill:
                        rows.append(
                            (
                                Decimal(fill["cost"]) + Decimal(fill["fee"]),
                                Decimal(fill["follower_pnl"]),
                            )
                        )
                sink.append(rows)
            fold[f"{label}_test_trades"] = (
                sum(len(r) for r in sink[-len(bucket) :]) if bucket else 0
            )
        folds.append(fold)
    top_nonempty = [r for r in top_rows if r]
    bottom_nonempty = [r for r in bottom_rows if r]
    top_trades = sum(len(r) for r in top_rows)
    bottom_trades = sum(len(r) for r in bottom_rows)
    report: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "delay_seconds": delay_seconds,
        "fee_bps": str(settings.fee_bps),
        "folds": folds,
        "top_bucket": {
            "traders": len(top_nonempty),
            "settled_trades": top_trades,
            "pooled_net_follower_return": _bucket_return(top_rows),
        },
        "bottom_bucket": {
            "traders": len(bottom_nonempty),
            "settled_trades": bottom_trades,
            "pooled_net_follower_return": _bucket_return(bottom_rows),
        },
        "difference_ci95": None,
    }
    enough = (
        len(top_nonempty) >= MIN_TRADERS_PER_BUCKET
        and len(bottom_nonempty) >= MIN_TRADERS_PER_BUCKET
        and top_trades >= MIN_TEST_TRADES
        and bottom_trades >= MIN_TEST_TRADES
    )
    if not enough:
        report["verdict"] = "insufficient_evidence"
        report["statement"] = (
            "Not enough out-of-sample settled follower trades to say whether top-scored traders "
            "beat bottom-scored ones after costs. No advantage is claimed."
        )
        return report
    interval = bootstrap_difference(top_nonempty, bottom_nonempty, seed=seed, resamples=resamples)
    report["difference_ci95"] = (
        None if interval is None else [round(interval[0], 6), round(interval[1], 6)]
    )
    if interval is None:
        report["verdict"] = "insufficient_evidence"
        report["statement"] = "Interval could not be estimated. No advantage is claimed."
    elif interval[0] > 0:
        report["verdict"] = "top_beats_bottom"
        report["statement"] = (
            "Out of sample, followers of top-scored traders beat followers of bottom-scored "
            "traders after fees and delay; the 95% interval is above zero."
        )
    elif interval[1] < 0:
        report["verdict"] = "bottom_beats_top"
        report["statement"] = (
            "Out of sample, followers of bottom-scored traders did better after costs. "
            "The score did not predict follower returns."
        )
    else:
        report["verdict"] = "no_difference_detected"
        report["statement"] = (
            "Top-scored traders' follower returns did NOT beat bottom-scored ones after costs "
            "beyond noise; the 95% interval includes zero. The score is not shown to help "
            "followers."
        )
    return report
