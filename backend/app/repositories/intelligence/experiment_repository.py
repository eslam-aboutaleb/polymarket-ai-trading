"""Experiment and model-calibration persistence queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.analytics.assessment import Assessment
from app.models.intelligence.governance import LLMExperiment
from app.repositories.intelligence.assessments import verified_resolution_filter


class ExperimentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_active_for_task(self, task: str) -> LLMExperiment | None:
        stmt = (
            select(LLMExperiment)
            .where(LLMExperiment.task == task, LLMExperiment.status == "active")
            .order_by(LLMExperiment.id.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def has_active_conflict(self, task: str, exclude_id: int | None = None) -> bool:
        stmt = select(LLMExperiment.id).where(
            LLMExperiment.task == task,
            LLMExperiment.status == "active",
        )
        if exclude_id is not None:
            stmt = stmt.where(LLMExperiment.id != exclude_id)
        return self.db.execute(stmt.limit(1)).first() is not None

    def get_by_id(self, experiment_id: int) -> LLMExperiment | None:
        stmt = select(LLMExperiment).where(LLMExperiment.id == experiment_id).limit(1)
        return self.db.execute(stmt).scalars().first()

    def compare_arms(self, experiment_name: str, cutoff: datetime) -> list[Any]:
        # A row only counts as *resolved* when it carries a verified
        # binary label: the legacy resolver wrote raw Gamma labels
        # ("ALICE"), and those rows must not enter an arm average.
        verified = case((verified_resolution_filter(), Assessment.brier_score))
        stmt = (
            select(
                Assessment.variant,
                func.count(Assessment.id).label("samples"),
                func.count(verified).label("resolved"),
                func.avg(verified).label("mean_brier"),
                func.min(verified).label("best_brier"),
                func.max(verified).label("worst_brier"),
            )
            .where(
                Assessment.experiment_id == experiment_name,
                Assessment.variant.in_(("control", "variant")),
                Assessment.ai_probability.isnot(None),
                Assessment.created_at >= cutoff,
            )
            .group_by(Assessment.variant)
        )
        return list(self.db.execute(stmt))
