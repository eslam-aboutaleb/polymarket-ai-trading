"""Application services for user lookups that cross API boundaries."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.users.user import User
from app.repositories.users.user_repository import UserRepository


def get_user_by_id(db: Session, user_id: int | None) -> User | None:
    """Load one user for an API or background service."""
    if user_id is None:
        return None
    return UserRepository(db).find_by_id(user_id)
