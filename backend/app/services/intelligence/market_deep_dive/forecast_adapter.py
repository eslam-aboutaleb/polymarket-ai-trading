"""Read-only forecast credibility display over the existing forecast store (#22/#29).

No forecast is created or scored here and no second calibration database exists.
Probabilities only become ``validated`` when the existing ten-bin calibration
(``valuation_evidence.calibrated_probability``) has enough resolved,
non-duplicated history before the forecast time, and the resolution rule has
not changed since the forecast was recorded. Anything else stays
``unvalidated`` and the raw probability, if shown, is labelled experimental.
"""

import math
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analytics.assessment import (
    BINARY_NO_LABELS,
    BINARY_YES_LABELS,
    VERIFIED_SCORING_VERSION,
    Assessment,
)
from app.services.intelligence.market_deep_dive.schemas import ForecastDisplay
from app.services.intelligence.resolution_rule_models import ContractSpecification
from app.services.intelligence.resolution_rules import get_rule_snapshot_as_of
from app.services.intelligence.valuation_evidence import aware, calibrated_probability

MAX_FORECAST_AGE = timedelta(hours=24)
_EPS = 1e-9


def _label(row: Assessment) -> int | None:
    text = str(row.resolved_outcome or "").strip().lower()
    if text in BINARY_YES_LABELS:
        return 1
    if text in BINARY_NO_LABELS:
        return 0
    return None


def _loss(p: float, y: int) -> tuple[float, float]:
    p = min(max(p, _EPS), 1 - _EPS)
    return (p - y) ** 2, -(y * math.log(p) + (1 - y) * math.log(1 - p))


def cohort_scores(db: Session, forecast: Assessment, cutoff: datetime) -> dict[str, object]:
    """Brier and log-loss of the forecast model's resolved history, paired with the market price.

    Only rows resolved before ``cutoff`` and not for this market are used, one per
    condition. The market baseline uses only rows that also carry the contemporaneous
    market price, so both columns are computed over the same labelled set.
    """
    rows = db.scalars(
        select(Assessment)
        .where(
            Assessment.model_name == forecast.model_name,
            Assessment.task == forecast.task,
            Assessment.trade_history_id.is_(None),
            Assessment.scoring_version == VERIFIED_SCORING_VERSION,
            Assessment.exclusion_reason.is_(None),
            Assessment.ai_probability.isnot(None),
            Assessment.resolved_at < cutoff,
            Assessment.created_at < cutoff,
            Assessment.condition_id != forecast.condition_id,
        )
        .order_by(Assessment.created_at.desc(), Assessment.id.desc())
        .limit(10000)
    ).all()
    seen: set[str] = set()
    model_loss: list[tuple[float, float]] = []
    market_loss: list[tuple[float, float]] = []
    for row in rows:
        condition = str(row.condition_id or "").lower()
        y = _label(row)
        p = row.ai_probability
        if not condition or condition in seen or y is None or p is None or not 0 <= p <= 1:
            continue
        seen.add(condition)
        model_loss.append(_loss(float(p), y))
        mp = row.market_price_at_assessment
        if mp is not None and 0 <= mp <= 1:
            market_loss.append(_loss(float(mp), y))
    # Pair on the same rows: recompute the model figures over the market-priced subset.
    paired_model = []
    seen.clear()
    for row in rows:
        condition = str(row.condition_id or "").lower()
        y = _label(row)
        mp = row.market_price_at_assessment
        p = row.ai_probability
        if (
            not condition
            or condition in seen
            or y is None
            or p is None
            or mp is None
            or not 0 <= p <= 1
            or not 0 <= mp <= 1
        ):
            continue
        seen.add(condition)
        paired_model.append(_loss(float(p), y))

    def mean(values: list[tuple[float, float]], index: int) -> Decimal | None:
        return (
            Decimal(str(round(sum(v[index] for v in values) / len(values), 6))) if values else None
        )

    return {
        "resolved": len(model_loss),
        "paired": len(paired_model),
        "brier": mean(model_loss, 0),
        "log_loss": mean(model_loss, 1),
        "paired_model_brier": mean(paired_model, 0),
        "market_brier": mean(market_loss, 0),
        "market_log_loss": mean(market_loss, 1),
    }


def build_forecast_display(
    db: Session,
    spec: ContractSpecification,
    *,
    as_of: datetime,
    market_benchmark_p: Decimal | None,
    market_benchmark_observed_at: datetime | None,
) -> ForecastDisplay:
    """Latest recorded forecast for this condition at or before ``as_of``, honestly labelled."""
    condition = spec.condition_id.lower()
    forecast = db.scalars(
        select(Assessment)
        .where(
            Assessment.condition_id == condition,
            # Copy-trade evaluations belong to one user's trades; only scan verdicts are shared.
            Assessment.trade_history_id.is_(None),
            Assessment.ai_probability.isnot(None),
            Assessment.created_at <= as_of,
        )
        .order_by(Assessment.created_at.desc(), Assessment.id.desc())
        .limit(1)
    ).first()
    if forecast is None:
        return ForecastDisplay(
            validity_status="unavailable", validity_reasons=["NO_RECORDED_FORECAST"], label="none"
        )
    raw = Decimal(str(forecast.ai_probability))
    created = aware(forecast.created_at)
    reasons: list[str] = []
    base = {
        "raw_probability": raw if raw.is_finite() and 0 <= raw <= 1 else None,
        "model_id": forecast.model_name,
        "model_version": forecast.prompt_version,
        "forecast_id": forecast.id,
        "forecast_recorded_at": created,
        "market_benchmark_p": market_benchmark_p,
        "market_benchmark_observed_at": market_benchmark_observed_at,
    }
    if base["raw_probability"] is None:
        return ForecastDisplay(
            **{**base, "raw_probability": None},
            validity_status="unvalidated",
            validity_reasons=["ABSTAIN_INVALID_FORECAST"],
            label="none",
        )
    if as_of - created > MAX_FORECAST_AGE:
        reasons.append("FORECAST_STALE")
    # The forecast must have been made under the rule currently in force.
    prior_rule = get_rule_snapshot_as_of(db, condition, created)
    if prior_rule is None:
        reasons.append("RULE_BINDING_UNAVAILABLE")
    elif prior_rule.source_hash != spec.source_hash:
        reasons.append("RULE_CHANGED_SINCE_FORECAST")
    scores = None
    try:
        probability, low, high, calibration = calibrated_probability(db, forecast, as_of)
    except ValueError as exc:
        text = str(exc)
        reasons.append(text if text.startswith("ABSTAIN_") else "ABSTAIN_CALIBRATION_UNAVAILABLE")
        return ForecastDisplay(
            **base,
            validity_status="unvalidated",
            validity_reasons=list(dict.fromkeys(reasons)),
            label="experimental_unvalidated",
        )
    scores = cohort_scores(db, forecast, min(created, as_of))
    fields = {
        **base,
        "calibrated_probability": probability,
        "interval": (low, high),
        "calibration_cohort": (
            f"{forecast.model_name}/{forecast.task or 'unknown'}/bin{min(9, int(raw * 10))}"
        ),
        "calibration_report_id": str(calibration["training_sha256"])[:16],
        "resolved_sample_count": int(calibration["sample_count"]),
        "brier": scores["brier"],
        "log_loss": scores["log_loss"],
        "market_baseline_brier": scores["market_brier"],
        "market_baseline_log_loss": scores["market_log_loss"],
    }
    if reasons:
        return ForecastDisplay(
            **fields,
            validity_status="unvalidated",
            validity_reasons=list(dict.fromkeys(reasons)),
            label="experimental_unvalidated",
        )
    return ForecastDisplay(**fields, validity_status="validated", label="validated_model_forecast")
