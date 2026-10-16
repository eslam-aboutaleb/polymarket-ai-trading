"""Refresh-token lookup queries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.users.token import RefreshToken


class RefreshTokenRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def find_active_by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = (
            select(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.is_revoked.is_(False),
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def add(self, record: RefreshToken) -> RefreshToken:
        """Stage a refresh-token record in the caller's transaction."""
        self.db.add(record)
        return record
