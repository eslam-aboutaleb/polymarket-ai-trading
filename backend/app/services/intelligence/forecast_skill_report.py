"""Read-only forecast skill report built from recorded forecasts (issue #22).

Extends the existing assessments pipeline: it reads rows the recorder wrote and
the resolver scored, and runs them through :mod:`forecast_scoring`. It writes
nothing, creates no table, and has no order, signing or wallet capability.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.analytics.assessment import Assessment
from app.services.intelligence import forecast_scoring as fs
from app.services.intelligence.calibration_service import calibration_assessments
from app.services.intelligence.forecast_identity import ensure_utc
from app.services.intelligence.forecast_resolution import (
    ExclusionReason,
    is_verified_resolution,
    outcome_value,
)
from app.services.intelligence.model_drift import detect_drift
from app.utils.time import utc_now

SCHEMA_VERSION = "forecast-skill-report-v1"
HORIZON_BUCKETS = (
    ("under_1d", 0, 86400),
    ("1d_to_7d", 86400, 7 * 86400),
    ("over_7d", 7 * 86400, None),
)


def horizon_bucket(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    for name, low, high in HORIZON_BUCKETS:
        if seconds >= low and (high is None or seconds < high):
            return name
    return "unknown"


def to_score_input(row: Assessment) -> fs.PredictionScoreInput:
    """Map one stored forecast to the typed scoring contract (no imputation)."""
    verified = row.exclusion_reason is None and is_verified_resolution(
        row.brier_score, row.resolved_outcome, row.scoring_version
    )
    label = outcome_value(row.resolved_outcome) if verified else None
    matched = row.benchmark_price is not None and row.benchmark_observed_at is not None
    baseline = row.benchmark_price if matched else row.market_price_at_assessment
    return fs.PredictionScoreInput(
        forecast_id=str(row.forecast_uuid or f"row:{row.id}"),
        as_of=ensure_utc(row.prediction_instant or row.created_at),
        rule_hash=row.rule_hash,
        p=float(row.ai_probability) if row.ai_probability is not None else float("nan"),
        baseline_p=baseline,
        final_label=label,
        event_cluster=(row.event_group or row.condition_id or f"row:{row.id}"),
        source_quality="matched" if matched else "unverified",
        market_close_at=ensure_utc(row.market_close_at),
        resolved_at=ensure_utc(row.resolved_at),
        mapping_reversed=row.exclusion_reason == ExclusionReason.TOKEN_MAPPING_MISMATCH.value,
        model=row.model_name,
        group=horizon_bucket(row.horizon_seconds),
    )


def build_report(
    db: Session,
    *,
    window_days: int,
    scope: str,
    user_id: int,
    min_effective_events: int = fs.DEFAULT_MIN_EFFECTIVE_EVENTS,
    resamples: int = 500,
) -> dict[str, Any]:
    rows = calibration_assessments(db, window_days=window_days, scope=scope, user_id=user_id)
    inputs = [to_score_input(row) for row in rows]
    drift = detect_drift(db, window_days=max(3, window_days))
    drifted_models = {entry.get("model_name") for entry in drift["drifted"]}

    def evaluate(items):
        return fs.evaluate(items, min_effective_events=min_effective_events, resamples=resamples)

    overall = evaluate(inputs)
    by_model: dict[str, list] = defaultdict(list)
    by_horizon: dict[str, list] = defaultdict(list)
    for item in inputs:
        by_model[item.model or "unattributed"].append(item)
        by_horizon[item.group or "unknown"].append(item)
    models = {}
    for name, items in sorted(by_model.items()):
        report = evaluate(items)
        report["state"] = fs.research_only(report, name in drifted_models)
        models[name] = report
    return {
        "schema_version": SCHEMA_VERSION,
        "scorer_version": fs.SCORER_VERSION,
        "generated_at": utc_now().isoformat(),
        "window_days": window_days,
        "scope": scope,
        "as_of_audit": {
            "baseline": "benchmark_price observed at or before the prediction instant",
            "unverified_baseline_policy": (
                "market_price_at_assessment is kept but never enters the paired delta"
            ),
            "sources": ["assessments", "forecast_resolution_events"],
        },
        "overall": overall,
        "by_model": models,
        "by_horizon": {name: evaluate(items) for name, items in sorted(by_horizon.items())},
        "drift": {
            "drifted_count": drift["drifted_count"],
            "drifted_models": sorted(m for m in drifted_models if m),
        },
        "source_coverage": {
            "forecasts": len(inputs),
            "with_matched_baseline": sum(1 for i in inputs if i.source_quality == "matched"),
            "with_event_group": sum(1 for r in rows if r.event_group),
            "window_start": (utc_now() - timedelta(days=window_days)).isoformat(),
        },
        "execution_eligible": False,
        "feature_flag": "FORECAST_CALIBRATION_ENABLED",
    }
