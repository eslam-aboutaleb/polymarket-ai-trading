"""Polymarket market metadata queries."""

from sqlalchemy import select

from app.models.market_data.market import Market
from app.repositories.base import SQLAlchemyRepository


class MarketRepository(SQLAlchemyRepository[Market]):
    """Repository for Polymarket prediction markets."""

    model = Market

    def get_by_condition_id(self, condition_id: str) -> Market | None:
        stmt = select(Market).where(Market.id == condition_id).limit(1)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_active(self, limit: int = 100, offset: int = 0) -> list[Market]:
        stmt = select(Market).where(Market.status == "active").offset(offset).limit(limit)
        return list(self.db.execute(stmt).scalars().all())
