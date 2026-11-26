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
2. **Replay-safe identity (issue #60).** Every row carries a random
   ``forecast_uuid`` and a unique ``request_key``. The key is a caller
   idempotency key, or the SHA-256 of the full-precision decision inputs
   (condition, token, p interpretation, probability, price, model, task,
   prompt, rule hash, evidence snapshot, calibrator) plus the decision
   instant or replay window. A replayed SSE event therefore collapses to
   the one row it duplicates, while a revised forecast (even one that
   rounds to the same 0.1pp) is kept as its own sample. The previous
   same-day rounded value key could drop a real revision and could not tell
   a replay from a new day's decision.
"""

import json
import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.analytics.assessment import NULL_PROBABILITY_REASON, Assessment
from app.services.intelligence.forecast_identity import (
    P_YES,
    REPLAY_WINDOW_SECONDS,
    build_benchmark,
    ensure_utc,
    parse_instant,
    request_key,
)
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


def _existing_request(db: Session, key: str) -> Assessment | None:
    return db.query(Assessment).filter(Assessment.request_key == key).first()


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
    request_id: str | None = None,
    token_id: str | None = None,
    outcome_mapping: dict[str, str] | None = None,
    rule_hash: str | None = None,
    evidence_snapshot_id: str | None = None,
    calibrator_version: str | None = None,
    prediction_instant: Any = None,
    market_close_at: Any = None,
    event_group: str | None = None,
    benchmark_bid: Any = None,
    benchmark_ask: Any = None,
    benchmark_observed_at: Any = None,
    benchmark_source: str | None = None,
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
        request_id: Caller idempotency key. When given it alone (scoped to the
            market) is the replay identity.
        token_id: CLOB token the probability is about (the YES token).
        outcome_mapping: ``{token_id: outcome label}`` in force at decision time.
        rule_hash: Resolution-rule hash in force at decision time.
        evidence_snapshot_id: Evidence snapshot the forecast was grounded on.
        calibrator_version: Calibrator applied to the raw estimate, if any.
        prediction_instant: When the forecaster produced the number (UTC).
            Defaults to the recording time.
        market_close_at: Scheduled market end, for the horizon and late filter.
        event_group: Event the market belongs to (the independence unit).
        benchmark_bid / benchmark_ask / benchmark_observed_at / benchmark_source:
            The contemporaneous quote. A quote observed after the prediction is
            rejected; the price is never back-filled.

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
    model = (model_name or "").strip() or None
    task_key = (task or "").strip() or None
    provider_name = (provider or "").strip() or None
    version = prompt_version or current_prompt_version()

    recorded_at = datetime.now(UTC)
    explicit_instant = parse_instant(prediction_instant)
    instant = explicit_instant or recorded_at
    close_at = parse_instant(market_close_at)
    benchmark = build_benchmark(
        price=price,
        bid=benchmark_bid,
        ask=benchmark_ask,
        observed_at=benchmark_observed_at,
        source=(benchmark_source or "").strip() or None,
        prediction_instant=instant,
    )
    if benchmark.rejected_reason in ("quote_after_prediction", "crossed_quote"):
        logger.warning(
            "forecast_benchmark_rejected: %s",
            benchmark.rejected_reason,
            extra={"condition_id": market_key, "task": task},
        )
    token = (token_id or "").strip() or None

    keys = [
        request_key(
            request_id=request_id,
            condition_id=market_key,
            token_id=token,
            p_interpretation=P_YES,
            probability=probability,
            market_price=price,
            model=model,
            task=task_key,
            prompt_version=version,
            rule_hash=rule_hash,
            evidence_snapshot_id=evidence_snapshot_id,
            calibrator_version=calibrator_version,
            prediction_instant=explicit_instant,
            recorded_at=recorded_at,
            window_offset=offset,
        )
        for offset in (0, -1)
    ]
    # Inside the replay window an identical decision is a replay. A previous
    # window's key counts only while its row is younger than the window, so a
    # replay straddling a window boundary still collapses.
    current = _existing_request(db, keys[0])
    if current is not None:
        return None
    if request_id is None and explicit_instant is None:
        previous = _existing_request(db, keys[1])
        if previous is not None:
            created = ensure_utc(previous.created_at)
            if (
                created is not None
                and (recorded_at - created).total_seconds() < REPLAY_WINDOW_SECONDS
            ):
                return None
    key = keys[0]

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
        ai_probability=probability,
        market_price_at_assessment=price,
        model_name=model,
        provider=provider_name,
        prompt_version=version,
        experiment_id=(experiment_id or "").strip() or None,
        variant=(variant or "").strip() or None,
        llm_tier=(llm_tier or "").strip() or None,
        created_at=instant,
        forecast_uuid=str(uuid.uuid4()),
        request_key=key,
        token_id=token,
        p_interpretation=P_YES,
        outcome_mapping_json=(
            json.dumps(outcome_mapping, sort_keys=True) if outcome_mapping else None
        ),
        rule_hash=(rule_hash or "").strip() or None,
        evidence_snapshot_id=(evidence_snapshot_id or "").strip() or None,
        calibrator_version=(calibrator_version or "").strip() or None,
        prediction_instant=instant,
        market_close_at=close_at,
        horizon_seconds=(
            int((close_at - instant).total_seconds()) if close_at is not None else None
        ),
        event_group=(event_group or "").strip() or None,
        benchmark_price=benchmark.price if benchmark.is_matched else None,
        benchmark_bid=benchmark.bid if benchmark.is_matched else None,
        benchmark_ask=benchmark.ask if benchmark.is_matched else None,
        benchmark_observed_at=benchmark.observed_at if benchmark.is_matched else None,
        benchmark_source=benchmark.source if benchmark.is_matched else None,
        quote_age_seconds=benchmark.age_seconds if benchmark.is_matched else None,
    )
    try:
        db.add(record)
        db.commit()
    except IntegrityError:
        # A concurrent writer recorded the same request first: a duplicate.
        db.rollback()
        return None
    except Exception:
        db.rollback()
        logger.exception("Failed to record forecast verdict for %s", market_key)
        return None
    return record
