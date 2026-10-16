"""Repository queries for redemption attempts."""

from __future__ import annotations

from datetime import datetime

from app.models.trading.redemption_attempt import RedemptionAttempt
from app.repositories.base import SQLAlchemyRepository


class TradingRedemptionRepository(SQLAlchemyRepository[RedemptionAttempt]):
    model = RedemptionAttempt

    def count_recent_live_attempts(
        self, user_id: int, cutoff: datetime, statuses: tuple[str, ...]
    ) -> int:
        return (
            self.db.query(RedemptionAttempt)
            .filter(
                RedemptionAttempt.user_id == user_id,
                RedemptionAttempt.status.in_(statuses),
                RedemptionAttempt.created_at >= cutoff,
            )
            .count()
        )
