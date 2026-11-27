"""Forecast calibration scoring (issue #22), version ``forecast-scoring-v1``.

Pure, deterministic and side-effect free: no database, no network, no order,
signing or wallet capability. It scores stated P(YES) against finalized binary
labels and against the market-implied probability observed at the same
timestamp, and says explicitly when the evidence is too thin to claim skill.

Design rules (each has a test):

* Brier ``(p - y)^2`` is exact. Log loss clips ``p`` to ``[eps, 1 - eps]`` for
  the logarithm only; the clipping policy and the number of clipped forecasts
  are reported.
* Only finalized YES/NO labels are scored. Void, disputed, unresolved and
  ambiguous labels, forecasts made at or after the market close, and
  non-finite probabilities are excluded with a reason code, never imputed.
* Forecasts on one event are one independent trial. The unit of analysis is the
  event cluster (mean of its forecast scores); uncertainty comes from a seeded
  cluster bootstrap, so overlapping markets of one event cannot masquerade as
  independent evidence.
* The baseline is the market price observed at the forecast time. Forecasts
  without a matched baseline are reported but never enter the paired delta.
* Recalibration is fitted only on rows resolved at or before a cutoff and is
  evaluated on forecasts made after it (an untouched future holdout).
* Skill is never claimed from a good Brier alone: below ``min_effective_events``
  the paired result is ``insufficient_sample`` and the state is research only.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

SCORER_VERSION = "forecast-scoring-v1"
LOG_LOSS_EPSILON = 1e-6
CLIPPING_POLICY = f"p clipped to [{LOG_LOSS_EPSILON}, 1-{LOG_LOSS_EPSILON}] inside log loss only"
DEFAULT_MIN_EFFECTIVE_EVENTS = 30
DEFAULT_BOOTSTRAP_RESAMPLES = 2000
DEFAULT_BOOTSTRAP_SEED = 20261125
ECE_BIN_COUNTS = (5, 10, 20)


class Exclusion(StrEnum):
    INVALID_PROBABILITY = "invalid_probability"
    NO_FINAL_LABEL = "no_final_label"
    FORECAST_AFTER_CLOSE = "forecast_after_close"
    MISSING_AS_OF = "missing_as_of"
    REVERSED_OUTCOME_MAPPING = "reversed_outcome_mapping"


class SkillStatus(StrEnum):
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    NO_MATCHED_BASELINE = "no_matched_baseline"
    BETTER_THAN_MARKET = "better_than_market"
    WORSE_THAN_MARKET = "worse_than_market"
    NOT_DISTINGUISHABLE = "not_distinguishable"


@dataclass(frozen=True)
class PredictionScoreInput:
    """One forecast, as scored. Mirrors the issue's typed contract."""

    forecast_id: str
    as_of: datetime | None
    rule_hash: str | None
    p: float
    baseline_p: float | None
    #: 1.0 / 0.0 for a finalized YES / NO; ``None`` for void, disputed, unresolved.
    final_label: float | None
    event_cluster: str
    #: ``matched`` when the baseline was observed at ``as_of``, else ``unverified``.
    source_quality: str = "unverified"
    market_close_at: datetime | None = None
    resolved_at: datetime | None = None
    #: True when the recorded YES token no longer maps to YES at resolution.
    mapping_reversed: bool = False
    model: str | None = None
    group: str | None = None


def brier(p: float, y: float) -> float:
    if not _is_prob(p) or y not in (0.0, 1.0):
        raise ValueError("Brier needs p in [0, 1] and a binary label")
    return (p - y) ** 2


def log_loss(p: float, y: float, eps: float = LOG_LOSS_EPSILON) -> float:
    if not _is_prob(p) or y not in (0.0, 1.0):
        raise ValueError("log loss needs p in [0, 1] and a binary label")
    c = min(max(p, eps), 1.0 - eps)
    return -(y * math.log(c) + (1.0 - y) * math.log(1.0 - c))


def _is_prob(p: Any) -> bool:
    return (
        isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p) and 0 <= p <= 1
    )


@dataclass
class Prepared:
    accepted: list[PredictionScoreInput] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)


def prepare(inputs: list[PredictionScoreInput]) -> Prepared:
    """Apply the inclusion rules; every drop is counted under a reason code."""
    out = Prepared()

    def drop(reason: Exclusion) -> None:
        out.excluded[reason.value] = out.excluded.get(reason.value, 0) + 1

    for item in inputs:
        if not _is_prob(item.p):
            drop(Exclusion.INVALID_PROBABILITY)
        elif item.final_label not in (0.0, 1.0):
            drop(Exclusion.NO_FINAL_LABEL)
        elif item.mapping_reversed:
            drop(Exclusion.REVERSED_OUTCOME_MAPPING)
        elif item.as_of is None:
            drop(Exclusion.MISSING_AS_OF)
        elif item.market_close_at is not None and item.as_of >= item.market_close_at:
            drop(Exclusion.FORECAST_AFTER_CLOSE)
        else:
            out.accepted.append(item)
    return out


def _clusters(items: list[PredictionScoreInput]) -> dict[str, list[PredictionScoreInput]]:
    grouped: dict[str, list[PredictionScoreInput]] = defaultdict(list)
    for item in items:
        grouped[item.event_cluster].append(item)
    return grouped


def _cluster_means(items: list[PredictionScoreInput], fn) -> list[float]:
    return [sum(fn(i) for i in members) / len(members) for members in _clusters(items).values()]


def wilson_interval(successes: float, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    phat = successes / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def reliability(items: list[PredictionScoreInput], bins: int = 10) -> list[dict[str, Any]]:
    """Per-bin predicted vs observed frequency with explicit support and CI."""
    buckets: list[list[PredictionScoreInput]] = [[] for _ in range(bins)]
    for item in items:
        buckets[min(int(item.p * bins), bins - 1)].append(item)
    rows = []
    for index, members in enumerate(buckets):
        n = len(members)
        ci = wilson_interval(sum(m.final_label for m in members), n)
        rows.append(
            {
                "bin": index,
                "lower": index / bins,
                "upper": (index + 1) / bins,
                "count": n,
                "events": len({m.event_cluster for m in members}),
                "mean_p": sum(m.p for m in members) / n if n else None,
                "observed_frequency": sum(m.final_label for m in members) / n if n else None,
                "ci95": ci,
            }
        )
    return rows


def expected_calibration_error(items: list[PredictionScoreInput], bins: int = 10) -> float | None:
    if not items:
        return None
    total = len(items)
    return sum(
        row["count"] / total * abs(row["observed_frequency"] - row["mean_p"])
        for row in reliability(items, bins)
        if row["count"]
    )


def _bootstrap_ci(
    values: list[float], resamples: int, seed: int, alpha: float = 0.05
) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)  # noqa: S311  (seeded, deterministic bootstrap; not security-sensitive)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    lo = means[int((alpha / 2) * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (lo, hi)


def evaluate(
    inputs: list[PredictionScoreInput],
    *,
    min_effective_events: int = DEFAULT_MIN_EFFECTIVE_EVENTS,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Score a cohort against labels and the matched market baseline."""
    prepared = prepare(inputs)
    items = prepared.accepted
    clipped = sum(1 for i in items if i.p < LOG_LOSS_EPSILON or i.p > 1 - LOG_LOSS_EPSILON)
    events = _clusters(items)
    report: dict[str, Any] = {
        "scorer_version": SCORER_VERSION,
        "clipping_policy": CLIPPING_POLICY,
        "forecast_count": len(inputs),
        "scored_forecast_count": len(items),
        "effective_independent_events": len(events),
        "min_effective_events": min_effective_events,
        "excluded": dict(prepared.excluded),
        "clipped_for_log_loss": clipped,
        "evidence_class": "implemented_detector",
    }
    if not items:
        report.update(
            status=SkillStatus.INSUFFICIENT_SAMPLE.value, abstain_reason="NO_SCORABLE_FORECASTS"
        )
        return report
    mean_brier = sum(_cluster_means(items, lambda i: brier(i.p, i.final_label))) / len(events)
    mean_ll = sum(_cluster_means(items, lambda i: log_loss(i.p, i.final_label))) / len(events)
    mean_p = sum(_cluster_means(items, lambda i: i.p)) / len(events)
    mean_y = sum(_cluster_means(items, lambda i: i.final_label)) / len(events)
    report.update(
        brier=mean_brier,
        log_loss=mean_ll,
        brier_ci95=_bootstrap_ci(
            _cluster_means(items, lambda i: brier(i.p, i.final_label)), resamples, seed
        ),
        calibration_in_the_large=mean_y - mean_p,
        sharpness=sum(_cluster_means(items, lambda i: (i.p - mean_p) ** 2)) / len(events),
        reliability=reliability(items, 10),
        ece=expected_calibration_error(items, 10),
        ece_sensitivity={str(b): expected_calibration_error(items, b) for b in ECE_BIN_COUNTS},
        base_rate=mean_y,
        constant_base_rate_brier=mean_y * (1 - mean_y),
    )
    matched = [
        i
        for i in items
        if i.baseline_p is not None and i.source_quality == "matched" and _is_prob(i.baseline_p)
    ]
    report["matched_baseline_forecasts"] = len(matched)
    if not matched:
        report.update(
            status=SkillStatus.NO_MATCHED_BASELINE.value, abstain_reason="NO_MATCHED_BASELINE"
        )
        return report
    m_events = _clusters(matched)
    d_brier = _cluster_means(
        matched, lambda i: brier(i.baseline_p, i.final_label) - brier(i.p, i.final_label)
    )
    d_ll = _cluster_means(
        matched, lambda i: log_loss(i.baseline_p, i.final_label) - log_loss(i.p, i.final_label)
    )
    model_b = sum(_cluster_means(matched, lambda i: brier(i.p, i.final_label))) / len(m_events)
    market_b = sum(_cluster_means(matched, lambda i: brier(i.baseline_p, i.final_label))) / len(
        m_events
    )
    ci_b = _bootstrap_ci(d_brier, resamples, seed)
    ci_ll = _bootstrap_ci(d_ll, resamples, seed + 1)
    report.update(
        paired_events=len(m_events),
        model_brier_matched=model_b,
        market_brier=market_b,
        brier_skill=(1 - model_b / market_b) if market_b > 0 else None,
        paired_brier_delta=sum(d_brier) / len(d_brier),
        paired_brier_delta_ci95=ci_b,
        paired_log_loss_delta=sum(d_ll) / len(d_ll),
        paired_log_loss_delta_ci95=ci_ll,
        delta_convention="baseline minus model; positive means the model beat the market",
    )
    if len(m_events) < min_effective_events or ci_b is None:
        report.update(
            status=SkillStatus.INSUFFICIENT_SAMPLE.value, abstain_reason="LOW_EFFECTIVE_SAMPLE"
        )
    elif ci_b[0] > 0:
        report["status"] = SkillStatus.BETTER_THAN_MARKET.value
    elif ci_b[1] < 0:
        report["status"] = SkillStatus.WORSE_THAN_MARKET.value
    else:
        report["status"] = SkillStatus.NOT_DISTINGUISHABLE.value
    report["claims_alpha"] = False
    return report


def research_only(report: dict[str, Any], drift_flagged: bool) -> dict[str, Any]:
    """Final gate: drift or missing demonstrated skill keeps the model research-only.

    This never converts into a trade; it is a state label for the UI and for
    sizing gates (#159).
    """
    reasons = []
    if drift_flagged:
        reasons.append("MODEL_DRIFT")
    if report.get("status") != SkillStatus.BETTER_THAN_MARKET.value:
        reasons.append(str(report.get("abstain_reason") or report.get("status")))
    return {"research_only": bool(reasons), "reasons": reasons, "execution_eligible": False}


# ── time-forward recalibration ────────────────────────────────


def split_time_forward(
    inputs: list[PredictionScoreInput], cutoff: datetime
) -> tuple[list[PredictionScoreInput], list[PredictionScoreInput]]:
    """Fit set = labels known by ``cutoff``; holdout = forecasts made after it.

    A row whose resolution arrived after the cutoff (delayed or disputed) is in
    neither set when it was forecast before the cutoff: its label was not known
    at fitting time, so it can neither fit nor honestly validate.
    """
    fit, holdout = [], []
    for item in prepare(inputs).accepted:
        if item.resolved_at is not None and item.resolved_at <= cutoff and item.as_of <= cutoff:
            fit.append(item)
        elif item.as_of > cutoff:
            holdout.append(item)
    return fit, holdout


@dataclass(frozen=True)
class FittedCalibrator:
    method: str
    train_start: datetime | None
    train_end: datetime | None
    train_events: int
    knots: tuple[tuple[float, float], ...] = ()
    platt: tuple[float, float] | None = None

    def __call__(self, p: float) -> float:
        if self.method == "isotonic":
            xs = self.knots
            if p <= xs[0][0]:
                return xs[0][1]
            for (x0, y0), (x1, y1) in zip(xs, xs[1:], strict=False):
                if p <= x1:
                    return y0 if x1 == x0 else y0 + (y1 - y0) * (p - x0) / (x1 - x0)
            return xs[-1][1]
        a, b = self.platt
        c = min(max(p, LOG_LOSS_EPSILON), 1 - LOG_LOSS_EPSILON)
        z = a * math.log(c / (1 - c)) + b
        return 1 / (1 + math.exp(-max(-60.0, min(60.0, z))))


def fit_isotonic(fit: list[PredictionScoreInput]) -> FittedCalibrator:
    """Pool-adjacent-violators on (p, y) of the fit rows only."""
    if not fit:
        raise ValueError("no fit rows")
    pts = sorted((i.p, i.final_label) for i in fit)
    blocks: list[list[float]] = []  # [sum_y, count, last_x, first_x]
    for x, y in pts:
        blocks.append([y, 1, x, x])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            top = blocks.pop()
            blocks[-1][0] += top[0]
            blocks[-1][1] += top[1]
            blocks[-1][2] = top[2]
    knots: list[tuple[float, float]] = []
    for s, n, last_x, first_x in blocks:
        knots.append((first_x, s / n))
        if last_x != first_x:
            knots.append((last_x, s / n))
    return FittedCalibrator(
        "isotonic",
        min(i.as_of for i in fit),
        max(i.resolved_at for i in fit),
        len(_clusters(fit)),
        knots=tuple(knots),
    )


def fit_platt(fit: list[PredictionScoreInput], iterations: int = 50) -> FittedCalibrator:
    """Logistic recalibration ``sigmoid(a * logit(p) + b)`` by Newton steps."""
    if not fit:
        raise ValueError("no fit rows")
    a, b = 1.0, 0.0
    xs = [
        math.log(
            min(max(i.p, LOG_LOSS_EPSILON), 1 - LOG_LOSS_EPSILON)
            / (1 - min(max(i.p, LOG_LOSS_EPSILON), 1 - LOG_LOSS_EPSILON))
        )
        for i in fit
    ]
    ys = [i.final_label for i in fit]

    def nll(pa: float, pb: float) -> float:
        total = 0.0
        for x, y in zip(xs, ys, strict=True):
            z = max(-60.0, min(60.0, pa * x + pb))
            total += math.log1p(math.exp(-abs(z))) + max(z, 0.0) - y * z
        return total

    for _ in range(iterations):
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for x, y in zip(xs, ys, strict=True):
            q = 1 / (1 + math.exp(-max(-60.0, min(60.0, a * x + b))))
            w = max(q * (1 - q), 1e-9)
            g_a += (q - y) * x
            g_b += q - y
            h_aa += w * x * x
            h_ab += w * x
            h_bb += w
        h_aa += 1e-6
        h_bb += 1e-6
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        # Backtracking line search: a full Newton step can overshoot badly
        # when the logits take only a few distinct values.
        step, base = 1.0, nll(a, b)
        while step > 1e-6 and nll(a - step * da, b - step * db) > base:
            step /= 2
        if step <= 1e-6:
            break
        a, b = a - step * da, b - step * db
        if abs(step * da) + abs(step * db) < 1e-9:
            break
    return FittedCalibrator(
        "platt",
        min(i.as_of for i in fit),
        max(i.resolved_at for i in fit),
        len(_clusters(fit)),
        platt=(a, b),
    )


def recalibrate_holdout(
    inputs: list[PredictionScoreInput],
    cutoff: datetime,
    *,
    method: str = "isotonic",
    min_fit_events: int = DEFAULT_MIN_EFFECTIVE_EVENTS,
    **eval_kwargs: Any,
) -> dict[str, Any]:
    """Fit on the past, evaluate raw vs recalibrated on the untouched future."""
    fit, holdout = split_time_forward(inputs, cutoff)
    result: dict[str, Any] = {
        "method": method,
        "cutoff": cutoff.isoformat(),
        "fit_forecasts": len(fit),
        "holdout_forecasts": len(holdout),
        "fit_events": len(_clusters(fit)),
    }
    if len(_clusters(fit)) < min_fit_events or not holdout:
        result.update(status="insufficient_sample", abstain_reason="LOW_FIT_OR_HOLDOUT_SAMPLE")
        return result
    calibrator = fit_isotonic(fit) if method == "isotonic" else fit_platt(fit)
    adjusted = [
        PredictionScoreInput(**{**i.__dict__, "p": min(max(calibrator(i.p), 0.0), 1.0)})
        for i in holdout
    ]
    result.update(
        status="evaluated",
        train_span=[calibrator.train_start.isoformat(), calibrator.train_end.isoformat()],
        raw=evaluate(holdout, **eval_kwargs),
        recalibrated=evaluate(adjusted, **eval_kwargs),
    )
    return result
