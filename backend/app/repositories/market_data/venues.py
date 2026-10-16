"""Cached cross-venue market reads and snapshot persistence."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.market_data.venue_market import VenueMarket
from app.repositories.base import SQLAlchemyRepository
from app.schemas.market import NormalizedMarket


class VenueMarketRepository(SQLAlchemyRepository[VenueMarket]):
    """Repository for cached and normalized cross-venue market metadata."""

    model = VenueMarket

    def __init__(self, db: Session) -> None:
        super().__init__(db)

    def get_by_venue_and_market_id(self, venue: str, venue_market_id: str) -> VenueMarket | None:
        stmt = (
            select(VenueMarket)
            .where(
                VenueMarket.venue == venue.lower(),
                VenueMarket.market_id == venue_market_id,
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_for_venue(self, venue: str, limit: int = 100) -> list[VenueMarket]:
        stmt = (
            select(VenueMarket)
            .where(VenueMarket.venue == venue.lower())
            .order_by(VenueMarket.updated_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_market(self, venue: str, market_id: str) -> VenueMarket | None:
        return (
            self.db.query(VenueMarket)
            .filter(VenueMarket.venue == venue, VenueMarket.market_id == market_id)
            .first()
        )

    def write_snapshot(
        self, markets: list[NormalizedMarket], prices_json: dict[str, str], now: datetime
    ) -> int:
        if not markets:
            return 0
        venue = markets[0].venue
        live_ids: set[str] = set()
        for market in markets:
            row = self.get_market(market.venue, market.market_id)
            if row is None:
                row = VenueMarket(venue=market.venue, market_id=market.market_id)
                self.db.add(row)
            row.question = market.question
            row.prices_json = prices_json[market.market_id]
            row.volume_24h = market.volume_24h
            row.liquidity = market.liquidity
            row.end_date = market.end_date
            row.fetched_at = now
            live_ids.add(market.market_id)
        self.db.query(VenueMarket).filter(
            VenueMarket.venue == venue,
            VenueMarket.market_id.notin_(live_ids),
        ).delete(synchronize_session=False)
        self.db.commit()
        return len(markets)

    def list_markets(
        self, *, venue: str | None, escaped_query: str | None, limit: int
    ) -> list[VenueMarket]:
        query = self.db.query(VenueMarket)
        if venue:
            query = query.filter(VenueMarket.venue == venue)
        if escaped_query:
            query = query.filter(VenueMarket.question.ilike(f"%{escaped_query}%", escape="\\"))
        return (
            query.order_by(VenueMarket.liquidity.desc(), VenueMarket.market_id.asc())
            .limit(limit)
            .all()
        )

    def list_for_comparison(self, limit: int) -> list[VenueMarket]:
        return (
            self.db.query(VenueMarket)
            .order_by(
                VenueMarket.liquidity.desc(), VenueMarket.venue.asc(), VenueMarket.market_id.asc()
            )
            .limit(limit)
            .all()
        )
