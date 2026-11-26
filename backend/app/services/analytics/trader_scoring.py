"""Net-of-cost trader scoring (issue #158).

Win rate is a weak signal on binary markets: buying 95c favourites wins
often and earns almost nothing. This module scores a trader on what a
follower can actually care about:

* net PnL per dollar risked (after fees, supplied already net by the caller),
* maximum drawdown of the cumulative PnL curve,
* number of distinct resolved markets (breadth / sample size).

Small samples are never ranked: below ``MIN_RESOLVED_MARKETS`` distinct
resolved markets or ``MIN_COST_RISKED`` dollars risked the trader is
``insufficient_sample`` and has no score. Above the minimum, the observed
return is shrunk toward zero by ``n / (n + PRIOR_MARKETS)`` so a lucky short
record cannot outrank a long one. Win rate is reported for information only
and has zero weight.

Score (0-100), documented in ``docs/trader-scoring.md``::

    return_score  = 70 * clamp(shrunk_net_return / 0.10, 0, 1)
    drawdown_score = 20 * (1 - clamp(max_drawdown / total_cost / 0.5, 0, 1))
                     (only when shrunk_net_return > 0, else 0)
    breadth_score = 10 * clamp(distinct_markets / 100, 0, 1)
"""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MODEL_VERSION = "trader-score-v2-net-of-cost"
MIN_RESOLVED_MARKETS = 20
MIN_COST_RISKED = Decimal(100)
PRIOR_MARKETS = 30
FULL_RETURN = 0.10
MAX_DRAWDOWN_FRACTION = 0.5
FULL_BREADTH_MARKETS = 100

TIER_THRESHOLDS = {"S": 85, "A": 70, "B": 55, "C": 40, "D": 0}

Money = Annotated[Decimal, Field(max_digits=24, decimal_places=8)]


class ResolvedPosition(BaseModel):
    """One position in a resolved market. ``pnl`` is net of fees and costs."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    market_id: str = Field(min_length=1, max_length=200)
    cost: Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=8)]
    pnl: Money
    closed_at: AwareDatetime


def tier_for(score: float | None) -> str:
    if score is None:
        return "UNRANKED"
    for tier, threshold in sorted(TIER_THRESHOLDS.items(), key=lambda item: -item[1]):
        if score >= threshold:
            return tier
    return "D"


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def shrink(raw_return: float, distinct_markets: int, prior: int = PRIOR_MARKETS) -> float:
    """Shrink an observed return toward zero; weight grows with resolved markets."""
    if distinct_markets <= 0:
        return 0.0
    return raw_return * distinct_markets / (distinct_markets + prior)


def max_drawdown(positions: list[ResolvedPosition]) -> Decimal:
    """Largest peak-to-trough fall of cumulative PnL, ordered by close time.

    The curve starts at zero, so a trader whose first trades lose is already in
    drawdown. Ties on ``closed_at`` are ordered by market id for determinism.
    """
    cumulative = Decimal(0)
    peak = Decimal(0)
    worst = Decimal(0)
    for position in sorted(positions, key=lambda p: (p.closed_at, p.market_id)):
        cumulative += position.pnl
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


def combine_score(
    shrunk_return: float, drawdown_fraction: float | None, distinct_markets: int
) -> dict[str, float]:
    """The documented 0-100 composite. Unknown drawdown earns no drawdown credit."""
    return_score = 70.0 * _clamp(shrunk_return / FULL_RETURN)
    drawdown_score = 0.0
    if shrunk_return > 0 and drawdown_fraction is not None:
        drawdown_score = 20.0 * (1 - _clamp(drawdown_fraction / MAX_DRAWDOWN_FRACTION))
    breadth_score = 10.0 * _clamp(distinct_markets / FULL_BREADTH_MARKETS)
    total = return_score + drawdown_score + breadth_score
    return {
        "return_score": round(return_score, 1),
        "drawdown_score": round(drawdown_score, 1),
        "breadth_score": round(breadth_score, 1),
        "score": round(min(100.0, max(0.0, total)), 1),
    }


def _result(
    *,
    status: str,
    reasons: list[str],
    distinct_markets: int,
    cost: Decimal,
    raw: float | None,
    shrunk: float | None,
    drawdown: Decimal | None,
    win_rate: float | None,
    source: str,
) -> dict[str, Any]:
    drawdown_fraction = None
    if drawdown is not None and cost > 0:
        drawdown_fraction = float(drawdown / cost)
    parts = None
    score = None
    if status == "scored" and shrunk is not None:
        parts = combine_score(shrunk, drawdown_fraction, distinct_markets)
        score = parts["score"]
    return {
        "model_version": MODEL_VERSION,
        "status": status,
        "reasons": reasons,
        "score": score,
        "tier": tier_for(score),
        "components": parts,
        "net_return_per_dollar_raw": None if raw is None else round(raw, 6),
        "net_return_per_dollar_shrunk": None if shrunk is None else round(shrunk, 6),
        "max_drawdown": None if drawdown is None else str(drawdown),
        "max_drawdown_fraction": None if drawdown_fraction is None else round(drawdown_fraction, 6),
        "distinct_resolved_markets": distinct_markets,
        "cost_risked": str(cost),
        "win_rate": win_rate,
        "win_rate_weight": 0,
        "input_source": source,
    }


def score_resolved_positions(positions: list[ResolvedPosition]) -> dict[str, Any]:
    """Score a trader from per-position resolved results."""
    by_market: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for position in positions:
        entry = by_market[position.market_id]
        entry[0] += position.cost
        entry[1] += position.pnl
    markets = len(by_market)
    cost = sum((entry[0] for entry in by_market.values()), Decimal(0))
    pnl = sum((entry[1] for entry in by_market.values()), Decimal(0))
    win_rate = (
        round(sum(1 for entry in by_market.values() if entry[1] > 0) / markets, 4)
        if markets
        else None
    )
    reasons = []
    if markets < MIN_RESOLVED_MARKETS:
        reasons.append(f"fewer than {MIN_RESOLVED_MARKETS} distinct resolved markets")
    if cost < MIN_COST_RISKED:
        reasons.append(f"less than {MIN_COST_RISKED} risked")
    raw = float(pnl / cost) if cost > 0 else None
    shrunk = None if raw is None else shrink(raw, markets)
    return _result(
        status="insufficient_sample" if reasons else "scored",
        reasons=reasons,
        distinct_markets=markets,
        cost=cost,
        raw=raw,
        shrunk=shrunk,
        drawdown=max_drawdown(list(positions)) if positions else None,
        win_rate=win_rate,
        source="resolved_positions",
    )


def score_aggregates(
    *, total_pnl: float, volume: float, markets_traded: int, win_rate_pct: float | None
) -> dict[str, Any]:
    """Score from leaderboard aggregates, which carry no drawdown or per-market data.

    Volume is treated as dollars risked, an upper bound, so the return is a
    conservative approximation. Drawdown is unknown, so it earns no credit and
    the maximum aggregate score is 80.
    """
    cost = Decimal(str(max(volume or 0.0, 0.0)))
    markets = max(int(markets_traded or 0), 0)
    reasons = []
    if markets < MIN_RESOLVED_MARKETS:
        reasons.append(f"fewer than {MIN_RESOLVED_MARKETS} distinct resolved markets")
    if cost < MIN_COST_RISKED:
        reasons.append(f"less than {MIN_COST_RISKED} risked")
    raw = float(total_pnl or 0.0) / float(cost) if cost > 0 else None
    if raw is not None and not math.isfinite(raw):
        raw = None
    return _result(
        status="insufficient_sample" if reasons else "scored",
        reasons=reasons,
        distinct_markets=markets,
        cost=cost,
        raw=raw,
        shrunk=None if raw is None else shrink(raw, markets),
        drawdown=None,
        win_rate=None if win_rate_pct is None else round(win_rate_pct / 100, 4),
        source="leaderboard_aggregates",
    )
