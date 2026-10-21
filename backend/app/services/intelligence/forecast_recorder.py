"""Verdict capture for forecast calibration (plan 01).

Every AI verdict in this system states a probability; this module
persists that probability together with the market price at decision
time and the model that produced it, so the nightly resolution job
(``forecast_resolution``) can score it with a Brier score and the
calibration API can aggregate accuracy per model and per task.

Two invariants matter:

1. **No fabricated probabilities.** A verdict without a usable
   ``ai_probability`` (missing, non-numeric, or outside ``[0, 1]``
   after coercion) is not written at all, and the reason is logged so
   the drop is never silent. Aggregates filter on
   ``ai_probability IS NOT NULL`` and never default to 0.5 — legacy
   rows written before this module existed stay ``NULL`` forever and
   are excluded rather than backfilled. A stated certainty (exactly 0
   or 1) is a forecast the forecaster actually made: it is kept as
   stated and clipped only inside the log-loss computation.
2. **Idempotency.** The 20-minute opportunity cache
   (``analysis.OPPORTUNITY_CACHE_TTL``) replays SSE events without
   re-running the AI, so a naive writer would inflate the sample with
   identical verdicts. A row is skipped when one already exists with
   the same ``(condition_id, round(market_price, 3),
   round(probability, 3), model_name, task, UTC date)`` key. The
   probability and the task label are part of the key so a re-analysis
   that moves the forecast, or a second endpoint that scores the same
   market, is kept as its own sample instead of being silently
   dropped.
"""

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.analytics.assessment import NULL_PROBABILITY_REASON, Assessment
from app.repositories.intelligence.assessments import AssessmentRepository
from app.services.intelligence.prompt_version import current_prompt_version

logger = logging.getLogger(__name__)

# Placeholder written for the NOT NULL ``reasoning`` column: scan
# verdicts stream scores without their prose to the recorder, and the
# calibration loop never reads reasoning back.
_RECORD_REASONING = "Forecast captured for calibration scoring"


def _coerce_probability(value: Any) -> float | None:
    """Coerce to a float inside ``[0, 1]``; ``None`` otherwise.

    Mirrors ``analysis_client._opt_float`` so the recorder and the
    gRPC client agree on what counts as a usable forecast. A stated
    certainty (exactly 0 or 1) is kept: it is a forecast the forecaster
    actually stated, and it is clipped only inside the log-loss
    computation, never rewritten here.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number if 0.0 <= number <= 1.0 else None


def _coerce_price(value: Any) -> float | None:
    """Coerce a market price to a float inside ``[0, 1]``; ``None`` otherwise."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 1.0 else None


def _coerce_score(value: Any) -> float:
    """Coerce a legacy 0-100 score/confidence to a float (0.0 when unusable).

    ``confidence`` is never fabricated at a neutral 50: Kelly reads that
    column as a probability, and a fake value would silently poison it.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _find_duplicate(
    db: Session,
    condition_id: str,
    price_key: float | None,
    probability_key: float,
    model: str | None,
    task_key: str | None,
):
    """Return the id of a same-key row recorded since UTC midnight, if any."""
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return AssessmentRepository(db).find_duplicate_forecast(
        condition_id=condition_id,
        day_start=day_start,
        day_end=day_end,
        probability=probability_key,
        task=task_key,
        model=model,
        price=price_key,
    )


def record_verdict(
    db: Session,
    *,
    condition_id: str | None,
    task: str | None,
    model_name: str | None,
    provider: str | None,
    ai_probability: Any,
    market_price: Any,
    ai_score: Any = None,
    confidence: Any = None,
    recommendation: str | None = None,
    source: str = "scan",
    prompt_version: str | None = None,
    experiment_id: str | None = None,
    variant: str | None = None,
    llm_tier: str | None = None,
) -> Assessment | None:
    """Persist one AI verdict for later forecast scoring.

    Args:
        db: Active SQLAlchemy session.
        condition_id: Polymarket market condition id (resolution key).
            Rows without one cannot be resolved and are not written.
        task: Calibration task label (e.g. ``opportunity_scan``).
        model_name: Model that produced the verdict (``None``/empty when
            the backend never echoes one; kept for ``data_quality``).
        provider: Backend/model provider (``None``/empty when unknown).
        ai_probability: Forecast probability in ``[0, 1]``.
        market_price: YES price at decision time (``[0, 1]``).
        ai_score: Legacy 0-100 score (informational only).
        confidence: Legacy 0-100 confidence (informational only).
        recommendation: Verdict label (``buy``/``hold``/…).
        source: Emitting call site, recorded in ``reasoning``.
        prompt_version: Hash of the prompt store that produced this
            verdict (plan 04). When omitted it is resolved here, so an
            existing caller keeps recording without changing.
        experiment_id: A/B experiment this verdict belongs to, if any.
        variant: ``control`` or ``variant`` arm of that experiment.
        llm_tier: Model tier that produced the verdict, when known.

    Returns:
        The persisted :class:`Assessment`, or ``None`` when the verdict
        was skipped: no condition id, no usable probability, or a
        duplicate of a row already recorded today.
    """
    market_key = (condition_id or "").strip()
    if not market_key:
        return None

    probability = _coerce_probability(ai_probability)
    if probability is None:
        # No usable forecast: nothing to calibrate. Defaulting to 0.5
        # would fabricate a coin-flip sample, so skip instead — and say
        # why, so the drop is visible instead of silent.
        logger.warning(
            "forecast_probability_unavailable: %s",
            NULL_PROBABILITY_REASON,
            extra={
                "condition_id": market_key,
                "task": task,
                "model_name": model_name,
                "probability_null_reason": NULL_PROBABILITY_REASON,
            },
        )
        return None

    price = _coerce_price(market_price)
    # The stored values are the rounded dedupe keys (0.1¢ precision on
    # Polymarket prices, 0.1pp on the forecast) so a replayed verdict
    # matches the row it would duplicate.
    price_key = None if price is None else round(price, 3)
    probability_key = round(probability, 3)
    model = (model_name or "").strip() or None
    task_key = (task or "").strip() or None
    provider_name = (provider or "").strip() or None

    if _find_duplicate(db, market_key, price_key, probability_key, model, task_key) is not None:
        return None

    record = Assessment(
        # Scan verdicts have no source trade row (trade_history_id is
        # nullable for exactly this reason); copy-trade evaluations keep
        # writing their link in copy_trade_service._save_assessment.
        trade_history_id=None,
        condition_id=market_key,
        task=task_key,
        ai_score=_coerce_score(ai_score),
        reasoning=f"{_RECORD_REASONING} via {source or 'scan'}",
        recommendation=(recommendation or "hold")[:50],
        risk_level="unknown",
        confidence=_coerce_score(confidence),
        ai_probability=probability_key,
        market_price_at_assessment=price_key,
        model_name=model,
        provider=provider_name,
        prompt_version=(prompt_version or current_prompt_version()),
        experiment_id=(experiment_id or "").strip() or None,
        variant=(variant or "").strip() or None,
        llm_tier=(llm_tier or "").strip() or None,
    )
    try:
        db.add(record)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to record forecast verdict for %s", market_key)
        return None
    return record
