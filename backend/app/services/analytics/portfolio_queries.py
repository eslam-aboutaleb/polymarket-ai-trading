"""Portfolio read operations backed by portfolio repositories."""

from sqlalchemy.orm import Session

from app.models.trading.redemption_attempt import RedemptionAttempt
from app.repositories.portfolio.redemptions import RedemptionRepository


def list_redemption_history(
    db: Session, *, user_id: int, limit: int = 50
) -> list[RedemptionAttempt]:
    bounded_limit = max(1, min(int(limit), 200))
    return RedemptionRepository(db).list_for_user(user_id, bounded_limit)
