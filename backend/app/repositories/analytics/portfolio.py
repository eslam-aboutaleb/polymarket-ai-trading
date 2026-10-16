"""Portfolio snapshot and account lookup queries for analytics."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analytics.portfolio_snapshot import PortfolioSnapshot
from app.models.users.user import User
from app.models.users.user_settings import UserSettings


class PortfolioAnalyticsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_user_settings(self, user_id: int) -> UserSettings | None:
        return self.db.query(UserSettings).filter(UserSettings.user_id == user_id).first()

    def get_snapshot(self, user_id: int, bucket_hour: datetime) -> PortfolioSnapshot | None:
        return (
            self.db.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.user_id == user_id,
                PortfolioSnapshot.bucket_hour == bucket_hour,
            )
            .first()
        )

    def first_bankroll_since(self, user_id: int, since: datetime, *, paper: bool) -> float | None:
        stmt = (
            select(PortfolioSnapshot.bankroll)
            .where(
                PortfolioSnapshot.user_id == user_id,
                PortfolioSnapshot.captured_at >= since,
                PortfolioSnapshot.is_paper.is_(paper),
            )
            .order_by(PortfolioSnapshot.captured_at.asc())
            .limit(1)
        )
        value = self.db.execute(stmt).scalar_one_or_none()
        return float(value) if value is not None else None

    def list_users(self) -> list[User]:
        return self.db.query(User).all()

    def list_snapshots(
        self,
        user_id: int,
        *,
        is_paper: bool | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> list[PortfolioSnapshot]:
        query = self.db.query(PortfolioSnapshot).filter(PortfolioSnapshot.user_id == user_id)
        if is_paper is not None:
            query = query.filter(PortfolioSnapshot.is_paper == bool(is_paper))
        if start is not None:
            query = query.filter(PortfolioSnapshot.bucket_hour >= start)
        if end is not None:
            query = query.filter(PortfolioSnapshot.bucket_hour < end)
        return query.order_by(PortfolioSnapshot.bucket_hour.desc()).limit(limit).all()
