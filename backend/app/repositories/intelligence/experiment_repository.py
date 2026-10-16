"""Experiment and model-calibration persistence queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.intelligence.governance import LLMExperiment


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
        stmt = text(
            """
            SELECT variant,
                   COUNT(*) AS samples,
                   SUM(CASE WHEN brier_score IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   AVG(CASE WHEN brier_score IS NOT NULL THEN brier_score END) AS mean_brier,
                   MIN(brier_score) AS best_brier,
                   MAX(brier_score) AS worst_brier
              FROM assessments
             WHERE experiment_id = :experiment
               AND variant IN ('control', 'variant')
               AND ai_probability IS NOT NULL
               AND created_at >= :cutoff
             GROUP BY variant
            """
        )
        return list(self.db.execute(stmt, {"experiment": experiment_name, "cutoff": cutoff}))
