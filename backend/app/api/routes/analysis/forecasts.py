"""Evidence-grounded forecast generation endpoints (issue #4).

Read/record only: they generate a P(YES) estimate with evidence provenance, record it through
the existing forecast recorder and expose its evidence and calibration. No order, signing or
wallet capability; ``pre_trade_gate`` and ``live_trading_policy`` are not involved.
Behind ``FORECAST_GENERATION_ENABLED`` (default off).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.analytics.assessment import Assessment
from app.models.analytics.forecast_evidence import ForecastEvidenceSnapshot
from app.services.intelligence import forecast_generation as fg
from app.services.intelligence import forecast_scoring as fs
from app.services.intelligence.calibration_service import calibration_assessments
from app.services.intelligence.forecast_skill_report import horizon_bucket, to_score_input
from app.utils.database import get_db

router = APIRouter(
    prefix="/api/analysis/forecasts",
    tags=["forecast generation"],
    dependencies=[Depends(get_current_user_from_token)],
)


def enabled(current_user: dict = Depends(get_current_user_from_token)) -> None:
    """Flag gate plus admin: generated forecasts are not tied to a user's trades, so (like
    ``scope=global`` on the skill report) they are an admin research surface."""
    if not get_settings().forecast_generation_enabled:
        raise HTTPException(503, "Forecast generation disabled")
    if not current_user.get("is_admin"):
        raise HTTPException(403, "forecast generation requires an admin account")


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)
    domain: str = Field(max_length=253)
    title: str = Field(default="", max_length=500)
    text: str = Field(max_length=20000)
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    syndication_key: str | None = Field(default=None, max_length=128)


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    family: str = Field(min_length=1, max_length=100)
    raw_p: float | None = Field(default=None, ge=0, le=1)
    interval: tuple[float, float] | None = None


class EvaluateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str = Field(min_length=1, max_length=100)
    outcome_id: str = Field(min_length=1, max_length=100)
    rule_hash: str | None = Field(default=None, max_length=64)
    rule_status: str = Field(max_length=32)
    as_of: datetime
    market_prob: float | None = Field(default=None, ge=0, le=1)
    model_version: str = Field(min_length=1, max_length=100)
    prompt_version: str | None = Field(default=None, max_length=16)
    category: str | None = Field(default=None, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    horizon_seconds: int | None = Field(default=None, ge=0)
    event_group: str | None = Field(default=None, max_length=100)
    benchmark_bid: float | None = Field(default=None, ge=0, le=1)
    benchmark_ask: float | None = Field(default=None, ge=0, le=1)
    benchmark_observed_at: datetime | None = None
    benchmark_source: str | None = Field(default=None, max_length=40)
    evidence: list[EvidenceIn] = Field(default_factory=list, max_length=fg.MAX_EVIDENCE_ITEMS)
    models: list[ModelIn] = Field(min_length=1, max_length=16)
    record: bool = True


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise HTTPException(422, "timestamps must carry a timezone")
    return value


@router.post("/evaluate", dependencies=[Depends(enabled)])
def evaluate(
    payload: EvaluateIn,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
) -> dict:
    as_of = _aware(payload.as_of)
    request = fg.ForecastRequest(
        condition_id=payload.condition_id,
        outcome_id=payload.outcome_id,
        rule_hash=payload.rule_hash,
        rule_status=payload.rule_status,
        as_of=as_of,
        market_prob=payload.market_prob,
        model_version=payload.model_version,
        prompt_version=payload.prompt_version,
        category=payload.category,
        horizon_seconds=payload.horizon_seconds,
        event_group=payload.event_group,
        benchmark_bid=payload.benchmark_bid,
        benchmark_ask=payload.benchmark_ask,
        benchmark_observed_at=_aware(payload.benchmark_observed_at),
        benchmark_source=payload.benchmark_source,
        evidence=tuple(
            fg.EvidenceItem(
                e.source_id,
                e.domain,
                e.text,
                _aware(e.published_at),
                _aware(e.retrieved_at),
                e.syndication_key,
                e.title,
            )
            for e in payload.evidence
        ),
    )
    models = [fg.ModelCandidate(m.name, m.family, m.raw_p, m.interval) for m in payload.models]
    rows = calibration_assessments(
        db, window_days=365, scope="global", user_id=current_user["user_id"]
    )
    inputs = [to_score_input(r) for r in rows if r.task and r.task.startswith(fg.TASK_PREFIX)]
    past = [
        fg.PastResolved(
            r.model_name or "unknown",
            r.provider or r.model_name or "unknown",
            float(r.ai_probability),
            i.final_label,
            i.event_cluster,
            i.resolved_at,
        )
        for r, i in zip(
            [r for r in rows if r.task and r.task.startswith(fg.TASK_PREFIX)], inputs, strict=True
        )
        if i.final_label is not None and i.resolved_at is not None and r.ai_probability is not None
    ]
    estimate = fg.build_estimate(request, models, past_resolved=past, calibration_inputs=inputs)
    out = {"estimate": estimate.as_dict(), "forecast_id": None, "recorded": False}
    if payload.record and not estimate.abstain:
        row = fg.record_estimate(db, request, estimate)
        out.update(forecast_id=row.forecast_uuid, recorded=True)
    out["execution_eligible"] = False
    return out


@router.get("/calibration", dependencies=[Depends(enabled)])
def calibration(
    category: str | None = Query(default=None, max_length=24, pattern=r"^[A-Za-z0-9_-]+$"),
    horizon: Literal["under_1d", "1d_to_7d", "over_7d"] | None = None,
    window_days: int = Query(default=90, ge=1, le=365),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
) -> dict:
    wanted = fg.TASK_PREFIX + (f":{category}" if category else "")
    rows = [
        r
        for r in calibration_assessments(
            db, window_days=window_days, scope="global", user_id=current_user["user_id"]
        )
        if r.task
        and (r.task == wanted if category else r.task.startswith(wanted))
        and (horizon is None or horizon_bucket(r.horizon_seconds) == horizon)
    ]
    report = fs.evaluate([to_score_input(r) for r in rows])
    return {
        "schema_version": "forecast-generation-calibration-v1",
        "category": category,
        "horizon": horizon,
        "window_days": window_days,
        "report": report,
        "execution_eligible": False,
    }


@router.get("/{forecast_id}/evidence", dependencies=[Depends(enabled)])
def evidence(
    forecast_id: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
) -> dict:
    row = db.query(Assessment).filter(Assessment.forecast_uuid == forecast_id).first()
    if row is None:
        raise HTTPException(404, "Forecast not found")
    snapshot = (
        db.get(ForecastEvidenceSnapshot, row.evidence_snapshot_id)
        if row.evidence_snapshot_id
        else None
    )
    if snapshot is None:
        raise HTTPException(404, "No evidence snapshot recorded for this forecast")
    return {
        "forecast_id": forecast_id,
        "evidence_snapshot_id": snapshot.snapshot_id,
        "rule_hash": row.rule_hash,
        "forecast_probability": row.ai_probability,
        "market_price": row.market_price_at_assessment,
        "calibrator_version": row.calibrator_version,
        "prediction_instant": row.prediction_instant.isoformat()
        if row.prediction_instant
        else None,
        "snapshot_as_of": snapshot.as_of.isoformat(),
        "evidence": json.loads(snapshot.payload_json),
        "execution_eligible": False,
    }
