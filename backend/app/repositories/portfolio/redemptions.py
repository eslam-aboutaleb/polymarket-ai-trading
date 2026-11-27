"""Read-side redemption attempt history."""

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.trading.redemption_attempt import RedemptionAttempt


class RedemptionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_for_user(self, user_id: int, limit: int, *, account=None) -> list[RedemptionAttempt]:
        query = self.db.query(RedemptionAttempt).filter(RedemptionAttempt.user_id == user_id)
        if account is not None:
            scope = RedemptionAttempt.account_id == account.id
            if account.kind == "personal":
                scope = or_(scope, RedemptionAttempt.account_id.is_(None))
            query = query.filter(scope)
        return query.order_by(RedemptionAttempt.created_at.desc()).limit(limit).all()
