"""API-key lookup queries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.users.api_key import ApiKey
from app.utils.time import utc_now


class ApiKeyRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def candidates_for_prefix(self, prefix: str, limit: int) -> list[ApiKey]:
        stmt = (
            select(ApiKey)
            .where(ApiKey.key_prefix == prefix)
            .order_by(ApiKey.id.desc())
            .limit(limit)
        )
        return list(self.db.execute(stmt).scalars().all())

    def list_for_user(self, user_id: int) -> list[ApiKey]:
        stmt = (
            select(ApiKey)
            .where(ApiKey.user_id == user_id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_key_prefix(self, prefix: str) -> list[ApiKey]:
        """Return currently usable keys matching a public prefix."""
        now = utc_now()
        stmt = (
            select(ApiKey)
            .where(
                ApiKey.key_prefix == prefix,
                ApiKey.revoked_at.is_(None),
                (ApiKey.expires_at.is_(None) | (ApiKey.expires_at > now)),
            )
            .order_by(ApiKey.id.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def list_for_account(self, account_id: int) -> list[ApiKey]:
        stmt = (
            select(ApiKey).where(ApiKey.account_id == account_id).order_by(ApiKey.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_for_user(self, user_id: int, key_id: int) -> ApiKey | None:
        stmt = select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id).limit(1)
        return self.db.execute(stmt).scalars().first()

    def list_unrevoked_for_user(self, user_id: int) -> list[ApiKey]:
        stmt = select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        return list(self.db.execute(stmt).scalars().all())
