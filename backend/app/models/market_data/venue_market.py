"""Cached venue market rows (plan 05 Phase A).

One row per ``(venue, market_id)`` holding the latest normalized
snapshot the refresh loop wrote. It is a **cache of public venue data**,
not user-owned state: no ``user_id`` or ``account_id`` (master plan 5.1
covers user-owned tables) — the same shape ``whale_events`` uses for
global on-chain data.

The cross-venue comparison needs history, not just live calls: Phase B's
mismatch detection joins on these rows, and the UI's data-quality
warning reads ``fetched_at`` to tell "the adapter never fetched" from
"the venue has no such market". The unique ``(venue, market_id)`` makes
the refresh loop's write idempotent, and the ``(venue, fetched_at)``
index serves the per-venue freshness query.
"""

from sqlalchemy import Column, DateTime, Float, Index, Integer, String, Text, UniqueConstraint

from app.models.base import Base
from app.utils.time import utc_now


class VenueMarket(Base):
    """Latest normalized snapshot of one venue market."""

    __tablename__ = "venue_markets"
    # Matches the migration's unique constraint and index.
    __table_args__ = (
        UniqueConstraint("venue", "market_id", name="uq_venue_markets_venue_market"),
        Index("ix_venue_markets_venue_fetched", "venue", "fetched_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    venue = Column(String(40), nullable=False)
    market_id = Column(String(200), nullable=False)
    question = Column(String(500), nullable=False, default="")
    # {"yes": .., "no": .., "outcomes": [...], "url": ..,
    #  "is_real_money": .., "raw_hash": ..} — everything that is not a
    #  first-class column, so a schema tweak does not need a migration.
    prices_json = Column(Text, nullable=True)
    volume_24h = Column(Float, nullable=False, default=0.0)
    liquidity = Column(Float, nullable=False, default=0.0)
    end_date = Column(DateTime(timezone=True), nullable=True)
    fetched_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<VenueMarket(venue={self.venue}, market={self.market_id}, "
            f"fetched_at={self.fetched_at})>"
        )
