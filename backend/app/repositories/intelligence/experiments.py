"""Experiment listing queries used by analysis endpoints."""

from sqlalchemy.orm import Session

from app.models.intelligence.governance import LLMExperiment


class ExperimentQueryRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_experiments(self, task: str | None = None, limit: int = 200) -> list[LLMExperiment]:
        query = self.db.query(LLMExperiment)
        if task:
            query = query.filter(LLMExperiment.task == task)
        return query.order_by(LLMExperiment.id.desc()).limit(limit).all()
