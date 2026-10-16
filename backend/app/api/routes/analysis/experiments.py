"""LLM experiment admin API (plan 04).

Admin-only CRUD for the A/B arms plus a read-only arm comparison that
aggregates the calibration rows written under each arm. The comparison
refuses to declare a winner below ``MIN_RESOLVED_FOR_VERDICT`` resolved
samples per arm: an arm with six resolved markets is noise, and calling
it a winner would push a prompt change that is not one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.platform.settings import require_admin
from app.models.intelligence.governance import LLMExperiment
from app.services.intelligence.experiment_query_service import (
    list_experiments as query_experiments,
)
from app.services.platform.experiment_service import (
    compare_experiment_arms,
    create_experiment,
    set_experiment_status,
)
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis/experiments", tags=["experiments"])

# Minimum resolved verdicts per arm before a comparison verdict is
# trustworthy. Mirrors the drift detector's refusal to flag noise.
MIN_RESOLVED_FOR_VERDICT = 20


class ExperimentCreateRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=64)
    task: str = Field(..., min_length=1, max_length=50)
    control_config: dict | None = None
    variant_config: dict | None = None
    traffic_pct: int = Field(default=10, ge=1, le=100)


class ExperimentStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(active|paused|done)$")


def _experiment_dict(experiment: LLMExperiment) -> dict:
    return {
        "id": experiment.id,
        "name": experiment.name,
        "task": experiment.task,
        "control_config": experiment.control_config_json,
        "variant_config": experiment.variant_config_json,
        "traffic_pct": experiment.traffic_pct,
        "status": experiment.status,
        "created_by": experiment.created_by,
        "created_at": experiment.created_at.isoformat() if experiment.created_at else None,
        "updated_at": experiment.updated_at.isoformat() if experiment.updated_at else None,
    }


@router.get("")
async def list_experiments(
    task: str | None = Query(default=None),
    _admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """All experiments, newest first, optionally filtered by task."""
    rows = query_experiments(db, task=task, limit=200)
    return {"count": len(rows), "experiments": [_experiment_dict(r) for r in rows]}


@router.post("")
async def create_experiment_endpoint(
    body: ExperimentCreateRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create an experiment. Rejects a second active arm per task."""
    experiment, error = create_experiment(
        db,
        name=body.name,
        task=body.task,
        control_config=body.control_config,
        variant_config=body.variant_config,
        traffic_pct=body.traffic_pct,
        created_by=admin.get("wallet_address"),
    )
    if experiment is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error)
    return _experiment_dict(experiment)


@router.patch("/{experiment_id}")
async def update_experiment_status(
    experiment_id: int,
    body: ExperimentStatusRequest,
    _admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Pause, resume or retire an experiment."""
    experiment, error = set_experiment_status(db, experiment_id, body.status)
    if experiment is None:
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if error == "experiment not found"
                else status.HTTP_409_CONFLICT
            ),
            detail=error,
        )
    return _experiment_dict(experiment)


@router.get("/{experiment_name}/comparison")
async def get_experiment_comparison(
    experiment_name: str,
    window_days: int = Query(default=30, ge=1, le=365),
    _admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Arm-by-arm calibration comparison for one experiment."""
    report = compare_experiment_arms(db, experiment_name, window_days=window_days)
    arms = report.get("arms", {})
    control = arms.get("control", {})
    variant = arms.get("variant", {})
    if report.get("verdict") == "inconclusive" and (
        control.get("resolved", 0) < MIN_RESOLVED_FOR_VERDICT
        or variant.get("resolved", 0) < MIN_RESOLVED_FOR_VERDICT
    ):
        report["verdict"] = "insufficient_samples"
        report["min_resolved_for_verdict"] = MIN_RESOLVED_FOR_VERDICT
    return report
