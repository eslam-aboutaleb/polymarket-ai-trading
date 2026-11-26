"""Insert-only analysis history, owner-scoped reads, retention and forward evaluation (#33 D).

Forecast and outcome data stay in the existing assessment tables; a run only
references them. Scoring uses only runs whose market resolved *after* the run
was created, compares the model with the market price recorded at analysis time,
and reports an insufficient sample instead of a skill claim when the cohort is small.
"""

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.analytics.analysis_run import AnalysisRun
from app.models.analytics.assessment import (
    BINARY_NO_LABELS,
    BINARY_YES_LABELS,
    VERIFIED_SCORING_VERSION,
    Assessment,
)
from app.services.intelligence.market_deep_dive.schemas import MarketDeepDiveResult
from app.utils.time import utc_now

MIN_SCORED_RUNS = 20


def result_digest(result: MarketDeepDiveResult) -> str:
    payload = result.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def persist_run(
    db: Session,
    result: MarketDeepDiveResult,
    user_id: int,
    *,
    provider: str | None,
    model: str | None,
    first_fact_ms: int | None,
    latency_ms: int | None,
    retention_days: int,
) -> AnalysisRun:
    """Insert one immutable row for an authenticated actor. The caller owns the commit."""
    pivotal = [c for c in result.claims if c.pivotal]
    yes = result.price_and_cost.indicative_prices.get("YES") if result.price_and_cost else None
    forecast = result.forecast
    forecast_p = None
    if forecast and forecast.validity_status == "validated":
        forecast_p = forecast.calibrated_probability
    failed = bool(result.completeness.upstream_failures)
    run = AnalysisRun(
        analysis_id=result.analysis_id,
        user_id=user_id,
        condition_id=result.identity.condition_id,
        selected_token_id=result.identity.selected_token_id,
        snapshot_id=result.identity.snapshot_id,
        rules_hash=result.identity.rules_hash,
        prompt_version=result.versions.get("prompt_version"),
        provider=provider,
        model=model,
        forecast_id=forecast.forecast_id if forecast else None,
        forecast_p_yes=None if forecast_p is None else str(forecast_p),
        market_p_yes=None if yes is None else str(yes),
        evidence_refs_json=json.dumps(
            [
                {
                    "evidence_id": e.evidence_id,
                    "canonical_url": e.canonical_url,
                    "content_hash": e.content_hash,
                }
                for e in result.evidence
            ]
        ),
        decision=result.decision,
        reason_codes_json=json.dumps(result.reason_codes),
        completeness_json=result.completeness.model_dump_json(),
        result_sha256=result_digest(result),
        pivotal_claims=len(pivotal),
        unsupported_pivotal_claims=sum(c.attribution != "attributed" for c in pivotal),
        attributed_claims=sum(c.attribution == "attributed" for c in result.claims),
        total_claims=len(result.claims),
        first_fact_ms=first_fact_ms,
        latency_ms=latency_ms,
        status="partial_failure" if failed else "completed",
        created_at=result.created_at,
    )
    db.add(run)
    purge_expired_runs(db, user_id, retention_days, now=result.created_at)
    return run


def purge_expired_runs(
    db: Session, user_id: int, retention_days: int, *, now: datetime | None = None
) -> int:
    cutoff = (now or utc_now()) - timedelta(days=retention_days)
    outcome = db.execute(
        delete(AnalysisRun).where(AnalysisRun.user_id == user_id, AnalysisRun.created_at < cutoff)
    )
    return int(outcome.rowcount or 0)


def get_run(db: Session, analysis_id: str, user_id: int) -> AnalysisRun | None:
    """Another user's run is indistinguishable from a missing one."""
    return db.scalars(
        select(AnalysisRun).where(
            AnalysisRun.analysis_id == analysis_id, AnalysisRun.user_id == user_id
        )
    ).first()


def list_runs(db: Session, user_id: int, limit: int = 25) -> list[AnalysisRun]:
    return list(
        db.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.user_id == user_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(min(max(limit, 1), 100))
        )
    )


def run_view(run: AnalysisRun) -> dict[str, object]:
    return {
        "analysis_id": run.analysis_id,
        "condition_id": run.condition_id,
        "selected_token_id": run.selected_token_id,
        "snapshot_id": run.snapshot_id,
        "rules_hash": run.rules_hash,
        "prompt_version": run.prompt_version,
        "provider": run.provider,
        "model": run.model,
        "forecast_id": run.forecast_id,
        "evidence_refs": json.loads(run.evidence_refs_json),
        "decision": run.decision,
        "reason_codes": json.loads(run.reason_codes_json),
        "completeness": json.loads(run.completeness_json),
        "result_sha256": run.result_sha256,
        "status": run.status,
        "created_at": run.created_at.isoformat(),
    }


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def _label(value: str | None) -> int | None:
    text = str(value or "").strip().lower()
    if text in BINARY_YES_LABELS:
        return 1
    if text in BINARY_NO_LABELS:
        return 0
    return None


def forward_scores(db: Session, user_id: int) -> dict[str, object]:
    """Paired Brier of recorded model probability vs the market price at analysis time."""
    runs = list(
        db.scalars(
            select(AnalysisRun).where(
                AnalysisRun.user_id == user_id,
                AnalysisRun.forecast_id.isnot(None),
                AnalysisRun.forecast_p_yes.isnot(None),
                AnalysisRun.market_p_yes.isnot(None),
            )
        )
    )
    by_model: dict[str, list[tuple[float, float]]] = {}
    seen: set[str] = set()
    for run in runs:
        row = db.get(Assessment, run.forecast_id)
        if (
            row is None
            or row.resolved_at is None
            or row.scoring_version != VERIFIED_SCORING_VERSION
            or row.exclusion_reason is not None
            or run.condition_id in seen
        ):
            continue
        resolved = (
            row.resolved_at
            if row.resolved_at.tzinfo
            else row.resolved_at.replace(tzinfo=run.created_at.tzinfo)
        )
        if resolved <= run.created_at:  # forward-only: no label known when the run was made
            continue
        y = _label(row.resolved_outcome)
        if y is None:
            continue
        seen.add(run.condition_id)
        model_p, market_p = float(Decimal(run.forecast_p_yes)), float(Decimal(run.market_p_yes))
        by_model.setdefault(run.model or "unknown", []).append(
            ((model_p - y) ** 2, (market_p - y) ** 2)
        )
    cohorts = {}
    for model, pairs in by_model.items():
        n = len(pairs)
        model_brier = sum(p[0] for p in pairs) / n
        market_brier = sum(p[1] for p in pairs) / n
        cohorts[model] = {
            "n": n,
            "model_brier": round(model_brier, 6),
            "market_brier": round(market_brier, 6),
            "brier_skill_vs_market": (
                round(1 - model_brier / market_brier, 6)
                if n >= MIN_SCORED_RUNS and market_brier > 0
                else None
            ),
            "status": "scored" if n >= MIN_SCORED_RUNS else "insufficient_sample",
        }
    return {
        "minimum_scored_runs": MIN_SCORED_RUNS,
        "cohorts": cohorts,
        "note": "Positive skill means lower Brier than the market price at analysis time; "
        "samples below the minimum report no skill figure.",
    }


def quality_report(db: Session, user_id: int) -> dict[str, object]:
    runs = list(db.scalars(select(AnalysisRun).where(AnalysisRun.user_id == user_id)))
    total = len(runs)
    pivotal = sum(r.pivotal_claims for r in runs)
    claims = sum(r.total_claims for r in runs)
    reasons: Counter[str] = Counter()
    for r in runs:
        reasons.update(json.loads(r.reason_codes_json))
    decisions = Counter(r.decision for r in runs)
    latencies = [r.latency_ms for r in runs if r.latency_ms is not None]
    firsts = [r.first_fact_ms for r in runs if r.first_fact_ms is not None]
    try:
        forward = forward_scores(db, user_id)
    except SQLAlchemyError:
        forward = {"cohorts": {}, "note": "unavailable"}
    return {
        "report_version": "deep-dive-quality-v1",
        "runs": total,
        "decisions": dict(decisions),
        "abstention_rate": None if not total else round(decisions["abstain"] / total, 4),
        "reason_code_counts": dict(reasons),
        "unsupported_pivotal_claim_rate": None
        if not pivotal
        else round(sum(r.unsupported_pivotal_claims for r in runs) / pivotal, 4),
        "citation_attribution_coverage": None
        if not claims
        else round(sum(r.attributed_claims for r in runs) / claims, 4),
        "api_failure_rate": None
        if not total
        else round(sum(r.status != "completed" for r in runs) / total, 4),
        "latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
        "time_to_first_fact_ms": {"p50": _percentile(firsts, 0.5)},
        "forward_scoring": forward,
        "not_measured": [
            "resolution_extraction_correctness (needs a labelled rule set)",
            "citation_accuracy beyond verbatim attribution (needs human labels)",
            "abstention_appropriateness (needs resolved outcomes for abstained runs)",
            "user_interaction (no interaction events are collected)",
        ],
    }
