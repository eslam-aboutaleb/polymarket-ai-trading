"""Billing-facing account-owner lookups."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.users.user import User
from app.repositories.users.user_repository import UserRepository


def get_account_owner(db: Session, user_id: int) -> User | None:
    """Return the identity that owns a billing account."""
    return UserRepository(db).find_by_id(user_id)


def update_account_owner_email(db: Session, user: User, email: str) -> User:
    """Persist the receipt email at checkout's existing commit boundary."""
    return UserRepository(db).update_email(user, email)
