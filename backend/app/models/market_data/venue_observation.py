"""Append-only public stream receipts; never private account data."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text

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
    __table_args__ = (
        Index("ix_venue_observation_token_clock", "token_id", "received_at", "id"),
        Index("ix_venue_observation_condition_clock", "condition_id", "received_at"),
        Index("ix_venue_observation_kind_clock", "token_id", "kind", "received_at"),
    )


class ObservationDataset(Base):
    __tablename__ = "observation_datasets"
    id = Column(String(64), primary_key=True)
    manifest_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class ObservationDatasetMember(Base):
    __tablename__ = "observation_dataset_members"
    dataset_id = Column(String(64), ForeignKey("observation_datasets.id"), primary_key=True)
    observation_id = Column(String(64), ForeignKey("venue_observations.id"), primary_key=True)
    __table_args__ = (Index("ix_dataset_member_observation", "observation_id"),)
