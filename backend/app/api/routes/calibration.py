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
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.config import get_settings
from app.models.assessment import Assessment
from app.models.user_trade import UserTrade
from app.services.forecast_resolution import outcome_value
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


def _scoped_rows(db: Session, window_days: int, scope: str, user_id: int):
    """Assessments with a usable probability inside the window, scoped."""
    cutoff = utc_now() - timedelta(days=max(1, window_days))
    query = db.query(Assessment).filter(
        Assessment.ai_probability.isnot(None),
        Assessment.created_at >= cutoff,
    )
    if scope != "global":
        # Inner join: only verdicts traceable to this user's copy trades.
        query = query.join(
            UserTrade,
            UserTrade.source_trade_history_id == Assessment.trade_history_id,
        ).filter(UserTrade.user_id == user_id)
    return query


def _legacy_excluded(db: Session, window_days: int, scope: str, user_id: int) -> int:
    """Count in-window rows that carry no probability (never backfilled)."""
    cutoff = utc_now() - timedelta(days=max(1, window_days))
    query = db.query(func.count(Assessment.id)).filter(
        Assessment.ai_probability.is_(None),
        Assessment.created_at >= cutoff,
    )
    if scope != "global":
        query = query.join(
            UserTrade,
            UserTrade.source_trade_history_id == Assessment.trade_history_id,
        ).filter(UserTrade.user_id == user_id)
    return int(query.scalar() or 0)


def _bucket_label(index: int) -> str:
    lower = index / _RELIABILITY_BUCKET_COUNT
    upper = (index + 1) / _RELIABILITY_BUCKET_COUNT
    return f"{lower:.1f}-{upper:.1f}"


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
        if row.brier_score is None or row.resolved_outcome is None:
            continue
        index = int(float(row.ai_probability) * _RELIABILITY_BUCKET_COUNT)
        index = min(max(index, 0), _RELIABILITY_BUCKET_COUNT - 1)
        bucket = buckets[index]
        bucket["count"] += 1
        bucket["_sum_p"] += float(row.ai_probability)
        # The YES/NO mapping is shared with the resolver so a market that
        # resolves to a named label lands in the same reliability bucket
        # the Brier scores already treat as a NO.
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
        if row.brier_score is not None:
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

    rows = _scoped_rows(db, window_days, scope, current_user["user_id"]).all()
    resolved = [row for row in rows if row.brier_score is not None]
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
            "legacy_excluded": _legacy_excluded(db, window_days, scope, current_user["user_id"]),
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

    rows = (
        _scoped_rows(db, window_days, scope, current_user["user_id"])
        .order_by(Assessment.created_at.desc())
        .limit(limit)
        .all()
    )
    forecasts = []
    for row in rows:
        price = row.market_price_at_assessment
        probability = row.ai_probability
        forecasts.append(
            {
                "condition_id": row.condition_id,
                "task": row.task,
                "model": row.model_name,
                "provider": row.provider,
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
