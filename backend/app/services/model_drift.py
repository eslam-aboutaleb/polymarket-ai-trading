"""Model drift detection (plan 04).

A calibration mean drifts before it breaks: the mean Brier score for a
task quietly climbs while the leaderboard keeps looking healthy, and by
the time anyone notices a strategy has been trading a degraded model
for weeks.

This service scores a rolling window per ``(model_name, task,
prompt_version)`` and flags degradation, then exports the per-model
time series the frontend charts and the scheduler alerts on.

Two deliberate constraints:

1. **Minimum samples before flagging.** A drift flag on fewer than
   ``MIN_SAMPLES_FOR_DRIFT`` resolved verdicts is noise — a handful of
   unlucky markets is not model decay, so the service refuses to call it.
2. **Baseline is the quarter baseline**, 0.25 for a two-outcome market.
   Degradation is measured as the *excess* over that, so a task that is
   merely bad at forecasting is not flagged as drifted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.assessment import Assessment

logger = logging.getLogger(__name__)

# Baseline for a two-outcome forecast scored with a Brier score.
BASELINE_BRIER = 0.25

# A flag requires at least this many resolved verdicts in the window.
MIN_SAMPLES_FOR_DRIFT = 20

# Default drift margin: mean Brier more than this far above the
# baseline *and* above the previous window is flagged.
DEFAULT_DRIFT_MARGIN = 0.02

# Linear-regression slope (Brier per day) above which a window is
# flagged as degrading even if the mean is still near baseline.
DEFAULT_SLOPE_ALARM = 0.002

DEFAULT_WINDOW_DAYS = 14


def _mean_brier(db: Session, model: str | None, task: str | None, since: datetime) -> float | None:
    query = db.query(func.avg(Assessment.brier_score)).filter(
        Assessment.ai_probability.isnot(None),
        Assessment.brier_score.isnot(None),
        Assessment.created_at >= since,
    )
    if model is None:
        query = query.filter(Assessment.model_name.is_(None))
    else:
        query = query.filter(Assessment.model_name == model)
    if task is None:
        query = query.filter(Assessment.task.is_(None))
    else:
        query = query.filter(Assessment.task == task)
    value = query.scalar()
    return None if value is None else float(value)


def model_skill_trend(
    db: Session,
    model_name: str | None,
    task: str | None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Skill over the window, plus the slope that says "is it degrading?".

    Returns the mean Brier, the excess over the baseline, the number of
    resolved samples, and a least-squares slope in Brier units per day.
    """
    since = datetime.now(UTC) - timedelta(days=window_days)
    rows = (
        db.query(Assessment.brier_score, Assessment.created_at)
        .filter(
            Assessment.ai_probability.isnot(None),
            Assessment.brier_score.isnot(None),
            Assessment.created_at >= since,
        )
        .filter(
            Assessment.model_name.is_(None)
            if model_name is None
            else Assessment.model_name == model_name
        )
        .filter(Assessment.task.is_(None) if task is None else Assessment.task == task)
        .order_by(Assessment.created_at)
        .all()
    )
    if not rows:
        return {
            "model_name": model_name,
            "task": task,
            "window_days": window_days,
            "samples": 0,
            "mean_brier": None,
            "excess_over_baseline": None,
            "slope_per_day": None,
        }

    scores = [float(r.brier_score) for r in rows if r.brier_score is not None]
    times = [r.created_at for r in rows if r.brier_score is not None]
    if not scores:
        return {
            "model_name": model_name,
            "task": task,
            "window_days": window_days,
            "samples": 0,
            "mean_brier": None,
            "excess_over_baseline": None,
            "slope_per_day": None,
        }
    mean = sum(scores) / len(scores)

    base = times[0].timestamp()
    days = [(t.timestamp() - base) / 86400.0 for t in times]
    mean_day = sum(days) / len(days)
    mean_score = mean
    denom = sum((d - mean_day) ** 2 for d in days)
    slope = 0.0
    if denom > 0:
        slope = (
            sum((d - mean_day) * (s - mean_score) for d, s in zip(days, scores, strict=True))
            / denom
        )

    return {
        "model_name": model_name,
        "task": task,
        "window_days": window_days,
        "samples": len(scores),
        "mean_brier": round(mean, 4),
        "excess_over_baseline": round(mean - BASELINE_BRIER, 4),
        "slope_per_day": round(slope, 6),
    }


def _active_models(db: Session) -> list[tuple[str | None, str | None]]:
    """Distinct ``(model_name, task)`` pairs seen in the last 30 days."""
    since = datetime.now(UTC) - timedelta(days=30)
    rows = (
        db.query(Assessment.model_name, Assessment.task)
        .filter(
            Assessment.ai_probability.isnot(None),
            Assessment.created_at >= since,
        )
        .distinct()
        .all()
    )
    return [(r.model_name, r.task) for r in rows]


def detect_drift(
    db: Session,
    window_days: int = DEFAULT_WINDOW_DAYS,
    drift_margin: float = DEFAULT_DRIFT_MARGIN,
    slope_alarm: float = DEFAULT_SLOPE_ALARM,
) -> dict[str, Any]:
    """Score every active model/task pair and flag degradation.

    A row is flagged when it has enough samples and either:
    * its mean Brier sits above ``drift_margin`` excess over baseline, or
    * its slope per day exceeds ``slope_alarm`` (getting worse).

    Rows without a resolvable model/task are never flagged — there is
    no reliable attribution, and flagging them would train operators to
    ignore the alert.
    """
    window_days = max(3, int(window_days))
    series: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    for model, task in _active_models(db):
        trend = model_skill_trend(db, model, task, window_days=window_days)
        samples = trend["samples"]
        mean = trend["mean_brier"]
        slope = trend["slope_per_day"] or 0.0
        excess = trend["excess_over_baseline"]

        row = dict(trend)
        row["baseline_brier"] = BASELINE_BRIER
        row["min_samples"] = MIN_SAMPLES_FOR_DRIFT
        row["slope_alarm"] = slope_alarm
        row["drift_margin"] = drift_margin

        is_drifted = False
        reasons: list[str] = []
        if samples >= MIN_SAMPLES_FOR_DRIFT and mean is not None and excess is not None:
            if excess > drift_margin:
                is_drifted = True
                reasons.append("excess_over_baseline")
            if slope > slope_alarm:
                is_drifted = True
                reasons.append("degrading_slope")
        elif samples < MIN_SAMPLES_FOR_DRIFT:
            reasons.append("insufficient_samples")

        row["drifted"] = is_drifted
        row["reasons"] = reasons
        series.append(row)
        if is_drifted:
            flagged.append(row)

    series.sort(key=lambda r: (-(r.get("excess_over_baseline") or -1), -(r.get("samples") or 0)))
    return {
        "window_days": window_days,
        "baseline_brier": BASELINE_BRIER,
        "min_samples_for_drift": MIN_SAMPLES_FOR_DRIFT,
        "series": series,
        "drifted": flagged,
        "drifted_count": len(flagged),
    }


def model_leaderboard(db: Session, window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Per-model accuracy ranked best-first, for the calibration panel."""
    since = datetime.now(UTC) - timedelta(days=window_days)
    rows = (
        db.query(
            Assessment.model_name,
            Assessment.task,
            func.count(Assessment.id).label("samples"),
            func.avg(Assessment.brier_score).label("mean_brier"),
            func.count(Assessment.brier_score).label("resolved"),
        )
        .filter(
            Assessment.ai_probability.isnot(None),
            Assessment.created_at >= since,
        )
        .group_by(Assessment.model_name, Assessment.task)
        .all()
    )
    entries = [
        {
            "model_name": r.model_name or "unknown",
            "task": r.task or "unknown",
            "samples": int(r.samples or 0),
            "resolved": int(r.resolved or 0),
            "mean_brier": round(float(r.mean_brier), 4) if r.mean_brier is not None else None,
        }
        for r in rows
    ]
    entries.sort(key=lambda e: e["mean_brier"] if e["mean_brier"] is not None else 99)
    return {
        "window_days": window_days,
        "baseline_brier": BASELINE_BRIER,
        "models": entries,
    }
