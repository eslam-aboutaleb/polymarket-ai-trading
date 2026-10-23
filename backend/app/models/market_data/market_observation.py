"""Public source observations. No private analysis, wallet or user data."""

from sqlalchemy import Column, DateTime, Index, String, Text

from app.models.base import Base


class MarketObservation(Base):
    __tablename__ = "market_observations"
    __table_args__ = (
        Index("ix_market_observations_condition_observed", "condition_id", "observed_at"),
    )

    snapshot_id = Column(String(64), primary_key=True)
    condition_id = Column(String(66), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    rule_hash = Column(String(64), nullable=False)
    # Store the exact normalized public response. Never recompute past state
    # from the current venue payload or insert fabricated historical rows.
    payload_json = Column(Text, nullable=False)
