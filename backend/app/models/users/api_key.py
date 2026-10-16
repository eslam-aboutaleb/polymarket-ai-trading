"""API key model — credential for the external API (plan 06 Phase B).

An ``ApiKey`` is how a non-browser client authenticates against
``/api/v1/external/*``. It belongs to one ``User`` and, when the caller
scoped it, to one of that user's ``Account`` rows, so an external client
is never a second identity: it is the same owner acting through a
channel that cannot hold a cookie.

Security invariants:

* Only the HMAC-SHA256 digest of the secret is persisted (see
  ``app.services.users.api_key_service``). The raw secret is returned exactly
  once, at creation, and is never stored, logged or re-derivable from
  this row.
* ``key_prefix`` is the first few characters of the secret. It exists so
  a lookup can be indexed and a digest comparison can be constant-time;
  it is treated as public.
* Scopes are an allow-list: ``read`` and ``trade`` only. There is
  deliberately no ``withdraw`` scope in v1, so no key can move funds
  anywhere the UI could not.
* ``revoked_at``/``expires_at`` are honoured on every authentication;
  a key with either set in the past is inert.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


class ApiKey(Base):
    """One hashed API key and the scopes it grants."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    # Owning identity. Both are indexed: every authentication filters on
    # the digest, every management route filters on the owner. No
    # cascade — deleting a user is not a supported operation.
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    #: Nullable per master 5.1: a key may be scoped to one account, or
    #: belong to the owner and read across the accounts it owns.
    account_id = Column(Integer, nullable=True, index=True)
    name = Column(String(100), nullable=False)
    #: First characters of the secret — the public half used for lookup.
    key_prefix = Column(String(32), nullable=False)
    #: HMAC-SHA256 hex digest of the secret. Never the secret itself.
    #: Unique and indexed: it is the only value an authentication ever
    #: matches on.
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    #: JSON array of granted scopes (subset of ``read``/``trade``).
    scopes = Column(Text, nullable=False, default="[]")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("ix_api_keys_user_created", "user_id", "created_at"),)

    def __repr__(self):
        return f"<ApiKey(id={self.id}, user={self.user_id}, prefix={self.key_prefix})>"
