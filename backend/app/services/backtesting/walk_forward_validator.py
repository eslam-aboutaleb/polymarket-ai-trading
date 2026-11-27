"""Walk-forward splits, purging, an untouched holdout and multiple-testing honesty."""

import math
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import NormalDist, mean, stdev
from typing import Any

from pydantic import model_validator

from app.services.backtesting.contracts import EngineConfig, Frozen, Identifier, ReplayEvent
from app.services.backtesting.event_replay_engine import (
    Decider,
    digest,
    events_snapshot_id,
    replay,
)

MIN_INDEPENDENT_GROUPS = 30


class HoldoutError(RuntimeError):
    """The holdout was already evaluated; further tuning would be contaminated."""


class WalkForwardConfig(Frozen):
    train_end: datetime
    validation_end: datetime
    end: datetime
    embargo_seconds: int = 0

    @model_validator(mode="after")
    def chronological(self):
        if not self.train_end < self.validation_end < self.end:
            raise ValueError("train_end < validation_end < end required")
        return self


class Candidate(Frozen):
    candidate_id: Identifier
    params: dict[str, Any] = {}


class ExperimentSpec(Frozen):
    experiment_id: Identifier
    snapshot_id: str
    primary_metric: str = "net_pnl"  # predeclared; the only selection criterion
    walk_forward: WalkForwardConfig
    config: EngineConfig
    candidates: list[Candidate]
    candidate_tokens: list[Identifier]  # candidate-set manifest, frozen up front
    alpha: float = 0.05

    @model_validator(mode="after")
    def validate_spec(self):
        if not 1 <= len(self.candidates) <= 50:
            raise ValueError("1..50 candidates required")
        ids = [c.candidate_id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate ids")
        if self.primary_metric != "net_pnl":
            raise ValueError("only the predeclared net_pnl metric is supported")
        return self


def _group_of(event: ReplayEvent) -> str:
    if event.group_id:
        return event.group_id
    return event.token_id if event.condition_id == "unknown" else event.condition_id


def split_signals(events: list[ReplayEvent], cfg: WalkForwardConfig) -> dict:
    """Split SIGNAL events by decision time with group purging and an embargo.

    * a group (market/event) lives in the segment of its first signal; later
      signals of the same group elsewhere are purged;
    * signals inside the embargo after a boundary are purged;
    * a signal whose group resolves after its segment ends is purged, so no
      label becomes known inside a later segment's training information.
    """
    signals = sorted(
        (e for e in events if e.kind == "SIGNAL"), key=lambda e: (e.available_at, e.event_id)
    )
    token_group = {e.token_id: _group_of(e) for e in signals}
    resolution: dict[str, datetime] = {}
    for e in events:
        if e.kind == "SETTLEMENT" and e.status in {"FINAL", "VOID"} and e.confirmed:
            group = token_group.get(e.token_id, e.token_id)
            resolution[group] = max(resolution.get(group, e.available_at), e.available_at)
    bounds = [("train", cfg.train_end), ("validation", cfg.validation_end), ("holdout", cfg.end)]
    embargo = timedelta(seconds=cfg.embargo_seconds)

    def segment(moment):
        for name, end in bounds:
            if moment < end:
                return name
        return None

    segments: dict[str, list[ReplayEvent]] = {"train": [], "validation": [], "holdout": []}
    purged: list[dict] = []
    home: dict[str, str] = {}
    starts = {"validation": cfg.train_end, "holdout": cfg.validation_end}
    ends = dict(bounds)
    for e in signals:
        name = segment(e.available_at)
        group = _group_of(e)
        if name is None:
            purged.append({"event_id": e.event_id, "reason": "after_experiment_end"})
            continue
        if name in starts and e.available_at < starts[name] + embargo:
            purged.append({"event_id": e.event_id, "reason": "embargo"})
            continue
        if home.setdefault(group, name) != name:
            purged.append({"event_id": e.event_id, "reason": "group_straddles_split"})
            continue
        if name != "holdout" and resolution.get(group, e.available_at) >= ends[name]:
            purged.append({"event_id": e.event_id, "reason": "label_resolves_after_segment"})
            continue
        segments[name].append(e)
    return {**segments, "purged": purged}


def _segment_events(events, signals, end):
    market = [e for e in events if e.kind != "SIGNAL" and e.available_at < end]
    return [*market, *signals]


def _significance(group_pnls: list[float], family_size: int, alpha: float, seed: int) -> dict:
    n = len(group_pnls)
    base = {"independent_groups": n, "family_size": family_size, "alpha": alpha}
    if n < MIN_INDEPENDENT_GROUPS:
        return {
            **base,
            "status": "disabled",
            "reason": f"insufficient_independent_groups_min_{MIN_INDEPENDENT_GROUPS}",
        }
    spread = stdev(group_pnls)
    if spread == 0:
        return {**base, "status": "disabled", "reason": "zero_variance"}
    t_stat = mean(group_pnls) / (spread / math.sqrt(n))
    p_one_sided = 1 - NormalDist().cdf(t_stat)
    rng = random.Random(seed)  # noqa: S311 - seeded research bootstrap
    means = sorted(mean(rng.choices(group_pnls, k=n)) for _ in range(500))
    return {
        **base,
        "status": "computed",
        "unit": "market_or_event_group_net_pnl",
        "mean": mean(group_pnls),
        "t_stat": t_stat,
        "p_value_one_sided_normal_approx": p_one_sided,
        "p_value_family_adjusted_bonferroni": min(1.0, p_one_sided * family_size),
        "mean_ci95_cluster_bootstrap": [means[12], means[487]],
        "significant_after_adjustment": min(1.0, p_one_sided * family_size) < alpha,
        "not_computed": ["probability_of_backtest_overfitting", "deflated_sharpe"],
    }


class WalkForwardValidator:
    """One validator instance owns one holdout and can read it exactly once."""

    def __init__(self, spec: ExperimentSpec, events: list[ReplayEvent]):
        if events_snapshot_id(events) != spec.snapshot_id:
            raise ValueError("experiment snapshot_id does not match the supplied events")
        self.spec, self.events = spec, events
        self.split = split_signals(events, spec.walk_forward)
        self._holdout_consumed = False
        self.attempts: list[dict] = []

    def _run(
        self, name: str, candidate: Candidate, factory: Callable[[dict], Decider], end
    ) -> dict:
        evs = _segment_events(self.events, self.split[name], end)
        return replay(
            evs,
            factory(candidate.params),
            self.spec.config,
            strategy_version=f"{self.spec.experiment_id}:{candidate.candidate_id}",
            parameters=candidate.params,
            mode=f"walk_forward_{name}",
        )

    def tune(self, factory: Callable[[dict], Decider]) -> dict:
        """Score every candidate on train and validation. Never touches the holdout."""
        if self._holdout_consumed:
            raise HoldoutError("holdout already consumed; tuning after evaluation is not allowed")
        cfg = self.spec.walk_forward
        self.attempts = []
        for candidate in self.spec.candidates:
            train = self._run("train", candidate, factory, cfg.train_end)
            valid = self._run("validation", candidate, factory, cfg.validation_end)
            self.attempts.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "params": candidate.params,
                    "train": _summary(train),
                    "validation": _summary(valid),
                }
            )
        scored = [a for a in self.attempts if a["validation"]["net_pnl"] is not None]
        selected = max(scored, key=lambda a: Decimal(a["validation"]["net_pnl"]), default=None)
        return {
            "attempts": self.attempts,
            "selected": None if selected is None else selected["candidate_id"],
            "selection_rule": "highest validation net_pnl among candidates with known net P&L",
            "unscorable": [
                a["candidate_id"] for a in self.attempts if a["validation"]["net_pnl"] is None
            ],
        }

    def evaluate_holdout(self, candidate_id: str, factory: Callable[[dict], Decider]) -> dict:
        if self._holdout_consumed:
            raise HoldoutError("holdout may be evaluated once per experiment")
        if not self.attempts or candidate_id not in {a["candidate_id"] for a in self.attempts}:
            raise ValueError("candidate must be tuned (train/validation) before holdout evaluation")
        self._holdout_consumed = True
        candidate = next(c for c in self.spec.candidates if c.candidate_id == candidate_id)
        report = self._run("holdout", candidate, factory, self.spec.walk_forward.end)
        groups = _group_pnls(report, self.events)
        return {
            "candidate_id": candidate_id,
            "report": report,
            "significance": _significance(
                groups, len(self.spec.candidates), self.spec.alpha, self.spec.config.seed
            ),
            "holdout_signal_count": len(self.split["holdout"]),
            "purged": self.split["purged"],
            "experiment_digest": digest(self.spec.model_dump(mode="json")),
        }


def _summary(report: dict) -> dict:
    return {
        "net_pnl": report["pnl"]["net_pnl"],
        "gross_pnl": report["pnl"]["gross_pnl"],
        "fees": report["pnl"]["fees"],
        "fills": len(report["fills"]),
        "quality_status": report["run"]["quality_status"],
        "result_hash": report["result_hash"],
    }


def _group_pnls(report: dict, events: list[ReplayEvent]) -> list[float]:
    token_group = {e.token_id: _group_of(e) for e in events if e.kind == "SIGNAL"}
    grouped: dict[str, Decimal] = {}
    for token, value in report["per_token_net"].items():
        group = token_group.get(token, token)
        grouped[group] = grouped.get(group, Decimal(0)) + Decimal(value)
    return [float(v) for v in grouped.values()]
