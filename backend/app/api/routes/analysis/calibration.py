"""Forecast calibration API (plan 01).

Read-only endpoints that score the AI's probability estimates against
market resolutions: Brier score versus the 0.25 coin-flip baseline, a
reliability curve (predicted vs actual frequency per probability
bucket), and per-model / per-task accuracy.

Every aggregate filters on ``ai_probability IS NOT NULL``: legacy
``Assessment`` rows written before the calibration columns existed
have no probability and are never backfilled (see
``forecast_recorder``). Rows lacking model attribution (cli-agent
verdicts, which never echo a model) are excluded from ``by_model`` and
counted in ``data_quality`` instead of being faked into a model group.

Scoping: copy-trade verdicts link to a user through
``Assessment.trade_history_id`` → ``UserTrade.source_trade_history_id``
→ ``UserTrade.user_id``. Opportunity-scan verdicts have no user link
yet, so the default (``scope=user``) covers the requesting user's
copy-trade verdicts only; ``scope=global`` aggregates every row and is
restricted to admins.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.analytics.assessment import Assessment
from app.services.intelligence.calibration_service import (
    calibration_assessments,
    legacy_assessment_count,
    save_verdict_feedback,
    verdict_feedback_summary,
)
from app.services.intelligence.calibration_service import (
    delete_verdict_feedback as delete_feedback,
)
from app.services.intelligence.forecast_resolution import outcome_value
from app.utils.database import get_db
from app.utils.time import utc_now

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/analysis/calibration", tags=["calibration"])

# A forecast that always says 50/50 scores 0.25 on the Brier scale.
BASELINE_BRIER = 0.25

# Reliability curve: 10 deciles of the (0, 1) probability range.
_RELIABILITY_BUCKET_COUNT = 10
# Model attribution that carries no usable model name (cli-agent never
# echoes one); excluded from by_model and surfaced in data_quality.
_UNATTRIBUTED_MODELS = {"", "unknown"}


def _bucket_label(index: int) -> str:
    lower = index / _RELIABILITY_BUCKET_COUNT
    upper = (index + 1) / _RELIABILITY_BUCKET_COUNT
    return f"{lower:.1f}-{upper:.1f}"


def _verified_resolution(row: Assessment) -> bool:
    """Only finalized binary YES/NO labels are eligible for calibration."""
    if row.brier_score is None or row.resolved_outcome is None:
        return False
    try:
        outcome_value(row.resolved_outcome)
    except ValueError:
        return False
    return True


def _reliability_buckets(rows: list[Assessment]) -> list[dict]:
    """Predicted vs actual frequency per probability decile (resolved rows).

    ``predicted`` is the mean stated probability in the bucket and
    ``actual`` the observed YES frequency; a perfectly calibrated
    forecaster puts every bucket on the y=x diagonal.
    """
    buckets = [
        {
            "bucket": _bucket_label(i),
            "predicted": None,
            "actual": None,
            "count": 0,
            "_sum_p": 0.0,
            "_sum_outcome": 0.0,
        }
        for i in range(_RELIABILITY_BUCKET_COUNT)
    ]
    for row in rows:
        if not _verified_resolution(row):
            continue
        index = int(float(row.ai_probability) * _RELIABILITY_BUCKET_COUNT)
        index = min(max(index, 0), _RELIABILITY_BUCKET_COUNT - 1)
        bucket = buckets[index]
        bucket["count"] += 1
        bucket["_sum_p"] += float(row.ai_probability)
        # The resolver validates outcome identity; named/legacy labels are
        # excluded rather than silently mapped to NO.
        bucket["_sum_outcome"] += outcome_value(row.resolved_outcome)

    result = []
    for bucket in buckets:
        count = bucket["count"]
        probability_sum = bucket.pop("_sum_p")
        outcome_sum = bucket.pop("_sum_outcome")
        if count:
            bucket["predicted"] = round(probability_sum / count, 4)
            bucket["actual"] = round(outcome_sum / count, 4)
        result.append(bucket)
    return result


def _group_stats(rows: list[Assessment], key: str) -> list[dict]:
    """Count + mean Brier per distinct attribution value (resolved subset)."""
    groups: dict[str, dict] = {}
    for row in rows:
        value = getattr(row, key) or ""
        group = groups.setdefault(value, {"key": value, "count": 0, "_sum": 0.0, "_resolved": 0})
        group["count"] += 1
        if _verified_resolution(row):
            group["_resolved"] += 1
            group["_sum"] += float(row.brier_score)
    stats = []
    for group in sorted(groups.values(), key=lambda g: (-g["count"], g["key"])):
        stats.append(
            {
                key: group["key"],
                "count": group["count"],
                "mean_brier": round(group["_sum"] / group["_resolved"], 4)
                if group["_resolved"]
                else None,
            }
        )
    return stats


@router.get("/summary")
async def get_calibration_summary(
    window_days: int = Query(default=settings.calibration_default_window_days, ge=1, le=365),
    scope: str = Query(default="user", pattern="^(user|global)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Brier score, reliability curve and per-model/task accuracy.

    Args:
        window_days: Rolling window ending now (default from settings).
        scope: ``user`` (default) scopes to the caller's copy-trade
            verdicts; ``global`` aggregates every verdict (admin only).
    """
    if scope == "global" and not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="scope=global requires an admin account",
        )

    rows = calibration_assessments(
        db,
        window_days=window_days,
        scope=scope,
        user_id=current_user["user_id"],
    )
    resolved = [row for row in rows if _verified_resolution(row)]
    invalid_legacy_resolutions = sum(
        1 for row in rows if row.brier_score is not None and not _verified_resolution(row)
    )
    attributed = [
        row for row in rows if (row.model_name or "").strip().lower() not in _UNATTRIBUTED_MODELS
    ]
    missing_model = len(rows) - len(attributed)
    mean_brier = (
        round(sum(float(row.brier_score) for row in resolved) / len(resolved), 4)
        if resolved
        else None
    )

    return {
        "window_days": window_days,
        "scope": scope,
        "generated_at": utc_now().isoformat(),
        "sample_count": len(rows),
        "resolved_count": len(resolved),
        "mean_brier": mean_brier,
        "brier_vs_baseline": BASELINE_BRIER,
        "reliability": _reliability_buckets(rows),
        "by_model": _group_stats(attributed, "model_name"),
        "by_task": _group_stats(rows, "task"),
        "data_quality": {
            "missing_model": missing_model,
            "invalid_legacy_resolution": invalid_legacy_resolutions,
            "legacy_excluded": legacy_assessment_count(
                db, window_days=window_days, scope=scope, user_id=current_user["user_id"]
            ),
        },
    }


@router.get("/forecasts")
async def get_calibration_forecasts(
    limit: int = Query(default=50, ge=1, le=500),
    window_days: int = Query(default=settings.calibration_default_window_days, ge=1, le=365),
    scope: str = Query(default="user", pattern="^(user|global)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Recent verdicts with probability, market price, edge and Brier score.

    Args:
        limit: Maximum rows returned (most recent first).
        window_days: Rolling window ending now (default from settings).
        scope: ``user`` (default) or ``global`` (admin only).
    """
    if scope == "global" and not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="scope=global requires an admin account",
        )

    rows = calibration_assessments(
        db,
        window_days=window_days,
        scope=scope,
        user_id=current_user["user_id"],
        limit=limit,
        newest_first=True,
    )
    forecasts = []
    for row in rows:
        price = row.market_price_at_assessment
        probability = row.ai_probability
        forecasts.append(
            {
                # The assessment id is what the feedback endpoints key
                # on, so a verdict row has to carry it or the rating UI
                # has nothing to attach to.
                "id": row.id,
                "condition_id": row.condition_id,
                "task": row.task,
                "model": row.model_name,
                "provider": row.provider,
                "prompt_version": row.prompt_version,
                "llm_tier": row.llm_tier,
                "ai_probability": probability,
                "market_price": price,
                "edge": round(probability - price, 4)
                if probability is not None and price is not None
                else None,
                "resolved_outcome": row.resolved_outcome,
                "brier_score": row.brier_score,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return {"count": len(forecasts), "forecasts": forecasts}


# ── Model governance (plan 04) ────────────────────────────────


@router.get("/summary/by-prompt-version")
async def get_calibration_by_prompt_version(
    window_days: int = Query(default=settings.calibration_default_window_days, ge=1, le=365),
    scope: str = Query(default="global", pattern="^(user|global)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Calibration grouped by prompt version, so prompt impact is visible.

    Answers "did the last prompt change help?", which is unanswerable
    without recording which prompt produced each verdict. Rows recorded
    before plan 04 have no ``prompt_version`` and appear under the
    ``(unversioned)`` key rather than being forced into a version group.
    """
    if scope == "global" and not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="scope=global requires an admin account",
        )

    rows = calibration_assessments(
        db,
        window_days=window_days,
        scope=scope,
        user_id=current_user["user_id"],
    )
    groups: dict[str, dict] = {}
    for row in rows:
        version = (row.prompt_version or "").strip() or "(unversioned)"
        group = groups.setdefault(
            version, {"prompt_version": version, "count": 0, "_sum": 0.0, "_resolved": 0}
        )
        group["count"] += 1
        if row.brier_score is not None:
            group["_resolved"] += 1
            group["_sum"] += float(row.brier_score)

    versions = [
        {
            "prompt_version": g["prompt_version"],
            "count": g["count"],
            "resolved": g["_resolved"],
            "mean_brier": round(g["_sum"] / g["_resolved"], 4) if g["_resolved"] else None,
        }
        for g in sorted(groups.values(), key=lambda g: (-g["count"], g["prompt_version"]))
    ]
    # Best (lowest Brier) version first, unversioned always last.
    versions.sort(key=lambda v: (v["mean_brier"] is None, v["mean_brier"] or 0.0))
    return {
        "window_days": window_days,
        "scope": scope,
        "baseline_brier": BASELINE_BRIER,
        "versions": versions,
    }


@router.get("/summary/drift")
async def get_calibration_drift(
    window_days: int = Query(default=14, ge=3, le=365),
    scope: str = Query(default="global", pattern="^(user|global)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Per-model/task calibration drift with the degrading slope (plan 04).

    A row is flagged only with at least ``min_samples_for_drift``
    resolved verdicts: a handful of unlucky markets is not model decay.
    """
    if scope == "global" and not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="scope=global requires an admin account",
        )

    from app.services.intelligence.model_drift import detect_drift

    report = detect_drift(db, window_days=window_days)
    report["scope"] = scope
    return report


@router.get("/leaderboard/models")
async def get_model_leaderboard(
    window_days: int = Query(default=settings.calibration_default_window_days, ge=1, le=365),
    scope: str = Query(default="global", pattern="^(user|global)$"),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Per-model accuracy, best-first, for the calibration panel."""
    if scope == "global" and not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="scope=global requires an admin account",
        )

    from app.services.intelligence.model_drift import model_leaderboard

    report = model_leaderboard(db, window_days=window_days)
    report["scope"] = scope
    return report


class VerdictFeedbackRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/verdicts/{assessment_id}/feedback")
async def submit_verdict_feedback(
    assessment_id: int,
    body: VerdictFeedbackRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Rate an AI verdict 1–5 (human-in-the-loop signal, plan 04).

    The rating never feeds back automatically; it is a prompt-authoring
    signal, so the row is immutable once written and a second rating for
    the same verdict replaces the previous one only through the delete
    endpoint below.
    """
    feedback = save_verdict_feedback(
        db,
        assessment_id=assessment_id,
        user_id=current_user["user_id"],
        rating=body.rating,
        comment=(body.comment or "").strip() or None,
    )
    if feedback is None:
        raise HTTPException(status_code=404, detail="Verdict not found")

    return {
        "id": feedback.id,
        "assessment_id": assessment_id,
        "rating": feedback.rating,
        "comment": feedback.comment,
    }


@router.delete("/verdicts/{assessment_id}/feedback")
async def delete_verdict_feedback(
    assessment_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Withdraw a rating (only the author's own)."""
    deleted = delete_feedback(db, assessment_id, current_user["user_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="No feedback to withdraw")
    return {"deleted": True}


@router.get("/verdicts/{assessment_id}/feedback")
async def get_verdict_feedback(
    assessment_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """The caller's own rating for a verdict, plus the community mean."""
    mine, aggregate = verdict_feedback_summary(db, assessment_id, current_user["user_id"])
    return {
        "assessment_id": assessment_id,
        "mine": {
            "rating": mine.rating,
            "comment": mine.comment,
        }
        if mine is not None
        else None,
        "community": {
            "count": int(aggregate[0] or 0),
            "mean_rating": round(float(aggregate[1]), 3) if aggregate[1] is not None else None,
        },
    }
