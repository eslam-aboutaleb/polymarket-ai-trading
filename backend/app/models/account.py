"""Account model — the single place trading ownership lives (plan 06 Phase A).

Identity stays strictly one user per wallet: a ``User`` is the login
principal, and an ``Account`` is the trading boundary underneath it. One
``personal`` account is created per user by the migration backfill, and
any number of ``sub`` accounts may hang off the same owner. Segregation
is what makes the rest of the system safe to scope: settings, balance
and P&L belong to an account, never to the wallet directly.

Ownership is ``owner_user_id`` only. There is deliberately no
relationship to the 21 user-owned tables and no cascade: the schema
stays additive, existing ``user_id`` columns keep working, and a nullable
``account_id`` is backfilled to the owner's own personal account.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String

from app.models.base import Base
from app.utils.time import utc_now


class AccountKind(StrEnum):
    """Which flavour of account this is.

    ``personal`` is the owner's own account: the migration creates
    exactly one per user and it is the default the ``X-Account-Id``
    header falls back to. ``sub`` accounts are opt-in trading
    boundaries created through the API.
    """

    PERSONAL = "personal"
    SUB = "sub"


class Account(Base):
    """A trading account owned by one user."""

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    # Indexed: every scope resolution and every list query filters on it.
    # No cascade — deleting a user is not a supported operation.
    owner_user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    name = Column(String(100), nullable=False)
    kind = Column(String(20), nullable=False, default=AccountKind.PERSONAL.value)
    # Exactly one default account per owner, enforced by a partial unique
    # index in the migration (application code on SQLite).
    is_default = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('personal', 'sub')",
            name="ck_accounts_kind",
        ),
    )

    def __repr__(self):
        return f"<Account(id={self.id}, owner={self.owner_user_id}, kind={self.kind})>"
