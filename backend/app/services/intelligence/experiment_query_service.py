"""Application read operation for the analysis experiment list."""

from sqlalchemy.orm import Session

from app.models.intelligence.governance import LLMExperiment
from app.repositories.intelligence.experiments import ExperimentQueryRepository


def list_experiments(
    db: Session, *, task: str | None = None, limit: int = 200
) -> list[LLMExperiment]:
    return ExperimentQueryRepository(db).list_experiments(task=task, limit=limit)
