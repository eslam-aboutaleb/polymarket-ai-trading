"""Append-only public stream receipts; never private account data."""

from sqlalchemy import Column, DateTime, Index, String, Text

from app.models.base import Base


class VenueObservation(Base):
    __tablename__ = "venue_observations"
    id = Column(String(64), primary_key=True)
    source = Column(String(40), nullable=False)
    connection_id = Column(String(36), nullable=False)
    token_id = Column(String(200), nullable=False)
    condition_id = Column(String(200))
    kind = Column(String(40), nullable=False)
    sequence = Column(String(100))
    event_at = Column(DateTime(timezone=True))
    received_at = Column(DateTime(timezone=True), nullable=False)
    stored_at = Column(DateTime(timezone=True), nullable=False)
    content_hash = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    normalized_json = Column(Text, nullable=False)
    quality_json = Column(Text, nullable=False)
    __table_args__ = (Index("ix_venue_observation_token_clock", "token_id", "received_at", "id"),)
