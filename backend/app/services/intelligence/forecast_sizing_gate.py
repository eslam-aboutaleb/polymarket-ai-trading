"""Skill gate for LLM-driven sizing (issue #159).

Pure functions that decide whether a forecast category may drive Kelly sizing, shrink the
estimate toward the market price with a fitted weight, and size on NET edge after fees.
No skill is assumed: a category is allowed only when the paired Brier delta against the
matched market baseline has a confidence interval that excludes zero in the model's favour,
on enough independent events, with no drift flag.

This module only RETURNS a sizing decision. It imports ``kelly_service`` read-only and edits
nothing in it, never touches ``pre_trade_gate`` or ``live_trading_policy`` and cannot place
orders. Wiring it into the live sizing call sites is a separate, explicitly-reviewed step.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from app.services.intelligence import forecast_scoring as fs
from app.services.trading import kelly_service

GATE_VERSION = "forecast-sizing-gate-v1"
FLAT_TINY_USDC = 1.0  # exchange minimum notional; "flat tiny" never exceeds it


@dataclass(frozen=True)
class CategoryDecision:
    category: str
    allowed: bool
    mode: str  # "kelly" | "flat_tiny" | "none"
    reasons: tuple[str, ...]
    status: str | None
    effective_events: int
    paired_brier_delta_ci95: tuple[float, float] | None


def category_reports(
    inputs: list[fs.PredictionScoreInput],
    *,
    min_effective_events: int = fs.DEFAULT_MIN_EFFECTIVE_EVENTS,
    resamples: int = fs.DEFAULT_BOOTSTRAP_RESAMPLES,
) -> dict[str, dict[str, Any]]:
    """Out-of-sample skill report per category (``PredictionScoreInput.group``).

    Rows without a category are reported under ``"uncategorised"`` and are never allowed.
    Callers must pass only forecasts resolved after the model was frozen (out of sample).
    """
    grouped: dict[str, list[fs.PredictionScoreInput]] = defaultdict(list)
    for item in inputs:
        grouped[item.group or "uncategorised"].append(item)
    return {
        cat: fs.evaluate(items, min_effective_events=min_effective_events, resamples=resamples)
        for cat, items in sorted(grouped.items())
    }


def decide(
    category: str,
    report: dict[str, Any] | None,
    *,
    drifted: bool = False,
    flat_tiny_allowed: bool = False,
) -> CategoryDecision:
    """Kelly only with demonstrated skill; otherwise flat tiny size or no trade."""
    reasons: list[str] = []
    status = (report or {}).get("status")
    ci = (report or {}).get("paired_brier_delta_ci95")
    events = int((report or {}).get("paired_events") or 0)
    if category in ("", "uncategorised"):
        reasons.append("NO_CATEGORY")
    if report is None:
        reasons.append("NO_SKILL_REPORT")
    elif status != fs.SkillStatus.BETTER_THAN_MARKET.value:
        reasons.append(f"SKILL_NOT_DEMONSTRATED:{status or 'unknown'}")
    if drifted:
        reasons.append("MODEL_DRIFT_DISABLED")
    allowed = not reasons
    if allowed:
        mode = "kelly"
    elif (
        flat_tiny_allowed and "MODEL_DRIFT_DISABLED" not in reasons and "NO_CATEGORY" not in reasons
    ):
        mode = "flat_tiny"
    else:
        mode = "none"
    return CategoryDecision(
        category=category,
        allowed=allowed,
        mode=mode,
        reasons=tuple(reasons),
        status=status,
        effective_events=events,
        paired_brier_delta_ci95=tuple(ci) if ci else None,
    )


def fit_shrinkage_weight(inputs: list[fs.PredictionScoreInput]) -> dict[str, Any]:
    """Weight ``w`` in ``m + w (p - m)`` fitted by least squares on past matched rows.

    ``w`` is clamped to [0, 1]: the LLM may never be extrapolated beyond its stated estimate.
    With no usable rows the weight is 0 (use the market price), never a made-up default.
    """
    rows = [
        i
        for i in inputs
        if i.final_label is not None
        and i.baseline_p is not None
        and i.source_quality == "matched"
        and fs.prepare([i]).accepted
    ]
    num = sum((i.p - i.baseline_p) * (i.final_label - i.baseline_p) for i in rows)
    den = sum((i.p - i.baseline_p) ** 2 for i in rows)
    if not rows or den == 0:
        return {"weight": 0.0, "fit_rows": len(rows), "reason": "NO_FIT_DATA"}
    return {"weight": min(1.0, max(0.0, num / den)), "fit_rows": len(rows), "reason": None}


def shrink(p_llm: float, p_market: float, weight: float) -> float:
    if not 0.0 <= weight <= 1.0:
        raise ValueError("weight must be in [0, 1]")
    return p_market + weight * (p_llm - p_market)


def net_edge(p: float, price: float, fee_per_share: float) -> float:
    """Expected profit per share of a YES buy after the taker fee (not gross ``p - price``)."""
    if fee_per_share < 0:
        raise ValueError("fee must be non-negative")
    return p - price - fee_per_share


def size_with_gate(
    decision: CategoryDecision,
    *,
    bankroll: float,
    p_llm: float,
    p_market: float,
    price: float,
    fee_per_share: float,
    weight: float,
    multiplier: float = kelly_service.DEFAULT_KELLY_MULTIPLIER,
    max_position_size: float | None = None,
) -> dict[str, Any]:
    """Return ``{size, mode, reasons, net_edge}``; size is 0 unless the gate allows it.

    Kelly runs on the shrunk estimate with the fee added to the cost per share, so the edge
    it sees is the net edge. A non-positive net edge sizes to 0.
    """
    p = shrink(p_llm, p_market, weight)
    edge = net_edge(p, price, fee_per_share)
    base = {
        "gate_version": GATE_VERSION,
        "mode": decision.mode,
        "reasons": list(decision.reasons),
        "shrunk_probability": p,
        "net_edge": edge,
        "execution_eligible": False,
    }
    if decision.mode == "none":
        return {**base, "size": 0.0}
    if edge <= 0:
        return {**base, "size": 0.0, "reasons": [*decision.reasons, "NO_NET_EDGE"]}
    if decision.mode == "flat_tiny":
        return {**base, "size": FLAT_TINY_USDC}
    cost = price + fee_per_share
    size = kelly_service.size(
        bankroll, p, cost, multiplier=multiplier, max_position_size=max_position_size
    )
    return {**base, "size": size}
