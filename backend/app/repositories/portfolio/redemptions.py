"""Read-side redemption attempt history."""

from sqlalchemy.orm import Session

from app.models.trading.redemption_attempt import RedemptionAttempt


class RedemptionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_for_user(self, user_id: int, limit: int) -> list[RedemptionAttempt]:
        return (
            self.db.query(RedemptionAttempt)
            .filter(RedemptionAttempt.user_id == user_id)
            .order_by(RedemptionAttempt.created_at.desc())
            .limit(limit)
            .all()
        )
