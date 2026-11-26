"""Out-of-sample, after-fee promotion gate for taker and maker strategies.

A strategy may leave paper only when its frozen parameters have produced enough
resolved paper trades inside one test window and the lower bound of a bootstrap
95% confidence interval on mean net return per dollar is above zero. Passing
the gate never implies profit; it only removes strategies with no evidence.

All inputs are plain data (resolved trades), so the gate is deterministic and
testable without a database or network.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

STRATEGIES = ("latency_arb", "maker", "copy", "inverse", "llm_sized")

DEFAULT_MIN_TRADES = 300
DEFAULT_BOOTSTRAP_SAMPLES = 2000
DEFAULT_CI_LEVEL = 0.95
DEFAULT_SEED = 155


@dataclass(frozen=True)
class ResolvedTrade:
    """One resolved trade, net of fees, per dollar of notional."""

    resolved_at: datetime
    params_hash: str
    gross_return: float  # per dollar, before fees
    fee_per_dollar: float
    notional: float = 1.0
    p_model: float | None = None  # forecast of the traded outcome (0-1)
    p_market: float | None = None  # market midpoint for the same outcome
    outcome: int | None = None  # 1 if the traded outcome won

    @property
    def net_return(self) -> float:
        return self.gross_return - self.fee_per_dollar


@dataclass(frozen=True)
class PromotionConfig:
    min_trades: int = DEFAULT_MIN_TRADES
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES
    ci_level: float = DEFAULT_CI_LEVEL
    seed: int = DEFAULT_SEED
    require_baseline: bool = False  # forecast strategies set this

    def __post_init__(self) -> None:
        if self.min_trades < 1:
            raise ValueError("min_trades must be positive")
        if not 0.5 < self.ci_level < 1:
            raise ValueError("ci_level must be in (0.5, 1)")
        if self.bootstrap_samples < 100:
            raise ValueError("bootstrap_samples must be at least 100")


@dataclass(frozen=True)
class PromotionWindow:
    """Frozen parameters and the instant the test clock started."""

    strategy: str
    params_hash: str
    started_at: datetime


def freeze_parameters(params: dict[str, Any]) -> str:
    """Stable SHA-256 of a parameter set (key order and whitespace irrelevant)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def open_window(
    strategy: str,
    params: dict[str, Any],
    now: datetime,
    previous: PromotionWindow | None = None,
) -> PromotionWindow:
    """Start a window, or keep the previous one if the parameters are unchanged.

    Any change to the parameters changes the hash and restarts the clock.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    digest = freeze_parameters(params)
    if previous and previous.strategy == strategy and previous.params_hash == digest:
        return previous
    return PromotionWindow(strategy, digest, now)


def bootstrap_mean_ci(
    values: Sequence[float], *, samples: int, level: float, seed: int
) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean; deterministic for a given seed."""
    if not values:
        raise ValueError("no values")
    rng = random.Random(seed)  # noqa: S311 - seeded statistics, not security
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    alpha = (1 - level) / 2
    lo = means[max(0, math.floor(alpha * samples))]
    hi = means[min(samples - 1, math.ceil((1 - alpha) * samples) - 1)]
    return lo, hi


def brier(forecasts: Iterable[float], outcomes: Iterable[int]) -> float:
    pairs = list(zip(forecasts, outcomes, strict=True))
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(forecasts: Iterable[float], outcomes: Iterable[int]) -> float:
    eps = 1e-6
    pairs = list(zip(forecasts, outcomes, strict=True))
    total = 0.0
    for p, o in pairs:
        p = min(1 - eps, max(eps, p))
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(pairs)


@dataclass(frozen=True)
class PromotionDecision:
    strategy: str
    passed: bool
    reasons: tuple[str, ...]
    window: PromotionWindow
    n: int
    excluded_other_parameters: int
    excluded_before_window: int
    min_trades: int
    mean_gross_return: float | None
    mean_fee: float | None
    mean_net_return: float | None
    ci_low: float | None
    ci_high: float | None
    ci_level: float
    baseline: dict[str, float] | None
    evidence_sha256: str = field(default="")

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "params_hash": self.window.params_hash,
            "window_started_at": self.window.started_at.isoformat(),
            "n": self.n,
            "min_trades": self.min_trades,
            "excluded_other_parameters": self.excluded_other_parameters,
            "excluded_before_window": self.excluded_before_window,
            "mean_gross_return": self.mean_gross_return,
            "mean_fee": self.mean_fee,
            "mean_net_return": self.mean_net_return,
            "ci_level": self.ci_level,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "baseline": self.baseline,
            "evidence_sha256": self.evidence_sha256,
            "note": "Passing this gate does not imply the strategy is or will be profitable.",
        }


def _baseline(trades: Sequence[ResolvedTrade]) -> dict[str, float] | None:
    rows = [
        t
        for t in trades
        if t.p_model is not None and t.p_market is not None and t.outcome is not None
    ]
    if not rows:
        return None
    model = [t.p_model for t in rows]
    market = [t.p_market for t in rows]
    outcome = [t.outcome for t in rows]
    return {
        "n": float(len(rows)),
        "brier_model": brier(model, outcome),
        "brier_market_midpoint": brier(market, outcome),
        "log_loss_model": log_loss(model, outcome),
        "log_loss_market_midpoint": log_loss(market, outcome),
    }


def evaluate_promotion(
    window: PromotionWindow,
    trades: Sequence[ResolvedTrade],
    config: PromotionConfig | None = None,
) -> PromotionDecision:
    """Decide whether a strategy may move from paper to the tiny live-cap stage."""
    cfg = config or PromotionConfig()
    other = [t for t in trades if t.params_hash != window.params_hash]
    early = [
        t
        for t in trades
        if t.params_hash == window.params_hash and t.resolved_at < window.started_at
    ]
    eligible = [
        t
        for t in trades
        if t.params_hash == window.params_hash and t.resolved_at >= window.started_at
    ]
    reasons: list[str] = []
    if other:
        reasons.append("PARAMETERS_CHANGED_IN_WINDOW")
    if len(eligible) < cfg.min_trades:
        reasons.append(f"INSUFFICIENT_SAMPLE: {len(eligible)} < {cfg.min_trades}")

    mean_gross = mean_fee = mean_net = ci_low = ci_high = None
    if eligible:
        net = [t.net_return for t in eligible]
        mean_gross = sum(t.gross_return for t in eligible) / len(eligible)
        mean_fee = sum(t.fee_per_dollar for t in eligible) / len(eligible)
        mean_net = sum(net) / len(net)
        ci_low, ci_high = bootstrap_mean_ci(
            net, samples=cfg.bootstrap_samples, level=cfg.ci_level, seed=cfg.seed
        )
        if len(eligible) >= cfg.min_trades and ci_low <= 0:
            reasons.append("CI_LOWER_BOUND_NOT_ABOVE_ZERO")

    baseline = _baseline(eligible)
    if cfg.require_baseline:
        if baseline is None:
            reasons.append("BASELINE_COMPARISON_MISSING")
        elif baseline["brier_model"] >= baseline["brier_market_midpoint"]:
            reasons.append("DOES_NOT_BEAT_MARKET_MIDPOINT")

    decision = PromotionDecision(
        strategy=window.strategy,
        passed=not reasons,
        reasons=tuple(reasons),
        window=window,
        n=len(eligible),
        excluded_other_parameters=len(other),
        excluded_before_window=len(early),
        min_trades=cfg.min_trades,
        mean_gross_return=mean_gross,
        mean_fee=mean_fee,
        mean_net_return=mean_net,
        ci_low=ci_low,
        ci_high=ci_high,
        ci_level=cfg.ci_level,
        baseline=baseline,
    )
    body = json.dumps(decision.to_dict(), sort_keys=True, default=str)
    return PromotionDecision(
        **{**decision.__dict__, "evidence_sha256": hashlib.sha256(body.encode()).hexdigest()}
    )


# ── Tiny live-cap stage and demotion ──────────────────────────────────────────


@dataclass(frozen=True)
class LiveCap:
    max_trade_notional: float
    max_daily_loss: float
    min_live_trades_for_demotion: int = 30

    def __post_init__(self) -> None:
        if self.max_trade_notional <= 0 or self.max_daily_loss <= 0:
            raise ValueError("live caps must be positive")


@dataclass(frozen=True)
class DemotionDecision:
    demote: bool
    reasons: tuple[str, ...]
    live_n: int
    live_mean_net_return: float | None
    paper_ci_low: float


def evaluate_demotion(
    decision: PromotionDecision,
    live_trades: Sequence[ResolvedTrade],
    cap: LiveCap,
    *,
    daily_pnl: float = 0.0,
) -> DemotionDecision:
    """Demote to paper when live results fall below the paper CI or caps are hit.

    ``daily_pnl`` is the day's live profit and loss in dollars (losses negative).
    """
    if not decision.passed or decision.ci_low is None:
        raise ValueError("only a passed promotion can be demoted")
    reasons: list[str] = []
    if any(t.notional > cap.max_trade_notional for t in live_trades):
        reasons.append("LIVE_NOTIONAL_CAP_BREACHED")
    if -daily_pnl >= cap.max_daily_loss:
        reasons.append("DAILY_LOSS_LIMIT_HIT")
    mean = None
    if live_trades:
        mean = sum(t.net_return for t in live_trades) / len(live_trades)
        if len(live_trades) >= cap.min_live_trades_for_demotion and mean < decision.ci_low:
            reasons.append("LIVE_BELOW_PAPER_CI")
    return DemotionDecision(bool(reasons), tuple(reasons), len(live_trades), mean, decision.ci_low)
