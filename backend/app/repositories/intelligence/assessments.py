"""Assessment, calibration and verdict-feedback queries."""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.analytics.assessment import Assessment
from app.models.intelligence.governance import VerdictFeedback
from app.models.trading.user_trade import UserTrade


class AssessmentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def find_duplicate_forecast(
        self,
        *,
        condition_id: str,
        day_start: datetime,
        day_end: datetime,
        probability: float,
        task: str | None,
        model: str | None,
        price: float | None,
    ) -> Assessment | None:
        query = self.db.query(Assessment.id).filter(
            Assessment.condition_id == condition_id,
            Assessment.created_at >= day_start,
            Assessment.created_at < day_end,
            Assessment.ai_probability == probability,
        )
        query = query.filter(Assessment.task.is_(None) if task is None else Assessment.task == task)
        query = query.filter(
            Assessment.model_name.is_(None) if model is None else Assessment.model_name == model
        )
        query = query.filter(
            Assessment.market_price_at_assessment.is_(None)
            if price is None
            else Assessment.market_price_at_assessment == price
        )
        return query.first()

    def pending_resolutions(self, limit: int) -> list[Assessment]:
        return (
            self.db.query(Assessment)
            .filter(
                Assessment.ai_probability.isnot(None),
                Assessment.condition_id.isnot(None),
                Assessment.condition_id != "",
                Assessment.condition_id.startswith("0x"),
                Assessment.resolved_at.is_(None),
            )
            .order_by(Assessment.created_at)
            .limit(limit)
            .all()
        )

    def scoped_calibration_rows(
        self,
        *,
        cutoff: datetime,
        scope: str,
        user_id: int,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Assessment]:
        query = self.db.query(Assessment).filter(
            Assessment.ai_probability.isnot(None), Assessment.created_at >= cutoff
        )
        if scope != "global":
            query = query.join(
                UserTrade, UserTrade.source_trade_history_id == Assessment.trade_history_id
            ).filter(UserTrade.user_id == user_id)
        if newest_first:
            query = query.order_by(Assessment.created_at.desc())
        if limit is not None:
            query = query.limit(limit)
        return query.all()

    def count_legacy_calibration_rows(self, *, cutoff: datetime, scope: str, user_id: int) -> int:
        query = self.db.query(func.count(Assessment.id)).filter(
            Assessment.ai_probability.is_(None), Assessment.created_at >= cutoff
        )
        if scope != "global":
            query = query.join(
                UserTrade, UserTrade.source_trade_history_id == Assessment.trade_history_id
            ).filter(UserTrade.user_id == user_id)
        return int(query.scalar() or 0)

    def exists(self, assessment_id: int) -> bool:
        return (
            self.db.query(Assessment.id).filter(Assessment.id == assessment_id).first() is not None
        )

    def mean_brier(self, *, model: str | None, task: str | None, since: datetime) -> float | None:
        query = self.db.query(func.avg(Assessment.brier_score)).filter(
            Assessment.ai_probability.isnot(None),
            Assessment.brier_score.isnot(None),
            Assessment.created_at >= since,
        )
        query = query.filter(
            Assessment.model_name.is_(None) if model is None else Assessment.model_name == model
        )
        query = query.filter(Assessment.task.is_(None) if task is None else Assessment.task == task)
        value = query.scalar()
        return None if value is None else float(value)

    def brier_series(
        self, *, model: str | None, task: str | None, since: datetime
    ) -> list[tuple[float, datetime]]:
        query = self.db.query(Assessment.brier_score, Assessment.created_at).filter(
            Assessment.ai_probability.isnot(None),
            Assessment.brier_score.isnot(None),
            Assessment.created_at >= since,
        )
        query = query.filter(
            Assessment.model_name.is_(None) if model is None else Assessment.model_name == model
        )
        query = query.filter(Assessment.task.is_(None) if task is None else Assessment.task == task)
        return query.order_by(Assessment.created_at).all()

    def active_models(self, since: datetime) -> list[tuple[str | None, str | None]]:
        rows = (
            self.db.query(Assessment.model_name, Assessment.task)
            .filter(Assessment.ai_probability.isnot(None), Assessment.created_at >= since)
            .distinct()
            .all()
        )
        return [(row.model_name, row.task) for row in rows]

    def model_leaderboard_rows(self, since: datetime):
        return (
            self.db.query(
                Assessment.model_name,
                Assessment.task,
                func.count(Assessment.id).label("samples"),
                func.avg(Assessment.brier_score).label("mean_brier"),
                func.count(Assessment.brier_score).label("resolved"),
            )
            .filter(Assessment.ai_probability.isnot(None), Assessment.created_at >= since)
            .group_by(Assessment.model_name, Assessment.task)
            .all()
        )

    def resolved_condition_rows(self, condition_ids: list[str]):
        return (
            self.db.query(
                Assessment.condition_id, Assessment.resolved_outcome, Assessment.created_at
            )
            .filter(
                Assessment.condition_id.in_(condition_ids), Assessment.resolved_outcome.isnot(None)
            )
            .order_by(Assessment.created_at.asc())
            .all()
        )

    def latest_ai_assessment(self, condition_id: str) -> Assessment | None:
        return (
            self.db.query(Assessment)
            .filter(Assessment.condition_id == condition_id, Assessment.ai_probability.isnot(None))
            .order_by(Assessment.created_at.desc())
            .first()
        )


class FeedbackRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_for_user(self, assessment_id: int, user_id: int) -> VerdictFeedback | None:
        return (
            self.db.query(VerdictFeedback)
            .filter(
                VerdictFeedback.assessment_id == assessment_id, VerdictFeedback.user_id == user_id
            )
            .first()
        )

    def delete_for_user(self, assessment_id: int, user_id: int) -> int:
        return int(
            self.db.query(VerdictFeedback)
            .filter(
                VerdictFeedback.assessment_id == assessment_id, VerdictFeedback.user_id == user_id
            )
            .delete()
        )

    def aggregate_for_assessment(self, assessment_id: int):
        return (
            self.db.query(func.count(VerdictFeedback.id), func.avg(VerdictFeedback.rating))
            .filter(VerdictFeedback.assessment_id == assessment_id)
            .first()
        )
