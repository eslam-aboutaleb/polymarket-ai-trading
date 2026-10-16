"""Append-only event log (plan 10 Phase A).

``AppEvent`` is an append-only log of the actions that move money or
change what the system is allowed to do. It sits *alongside* the current
tables — this is incremental event sourcing, not a rewrite: aggregates
stay where they are, and no historical rows are backfilled.

It exists because the only other audit surface is ``AdminAuditLog``
(admin promote/demote, written by ``main``'s env sync) and an in-memory
1000-entry request ring that dies with the process. Compliance
reporting, the strategy marketplace's verified track record and the
portfolio read models are all downstream of this table.

Only writes are defined here; nothing in the application updates or
deletes an ``AppEvent``. ``schema_version`` lets the payload shape
evolve without breaking a reader that predates it.
"""

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class AppEvent(Base):
    """One immutable record of something that happened."""

    __tablename__ = "app_events"

    id = Column(Integer, primary_key=True, index=True)
    # When the event happened (not when it was read back).
    occurred_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    # Owning identity. ``account_id`` is nullable and additive (master
    # plan 5.1): accounts land in plan 06 Phase A, and backfilling this
    # column for pre-account rows is a no-op because they map to the
    # owner's own personal account.
    user_id = Column(Integer, nullable=True, index=True)
    account_id = Column(Integer, nullable=True)
    aggregate_type = Column(String(50), nullable=False)  # trade | order | settings | credential
    aggregate_id = Column(String(200), nullable=True)
    event_type = Column(String(80), nullable=False)
    # JSON payload. No secrets, no PII: an event names ids and amounts,
    # never a private key, and never a full wallet address.
    payload_json = Column(Text, nullable=True)
    schema_version = Column(Integer, default=1, nullable=False)
    # Provenance for the audit reader: web | worker | scheduler | api
    source = Column(String(50), nullable=True)

    __table_args__ = (
        Index("ix_app_events_aggregate", "aggregate_type", "aggregate_id"),
        Index("ix_app_events_user_occurred", "user_id", "occurred_at"),
    )

    def __repr__(self):
        return (
            f"<AppEvent(id={self.id}, type={self.event_type}, "
            f"aggregate={self.aggregate_type}/{self.aggregate_id})>"
        )
