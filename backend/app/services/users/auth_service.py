"""Authentication persistence orchestration for user and refresh-token flows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.users.token import RefreshToken
from app.models.users.user import User
from app.repositories.users.user_repository import UserRepository
from app.security.refresh_tokens import (
    create_refresh_token_record,
    find_active_refresh_token_record,
)


def get_or_create_user_for_login(db: Session, wallet_address: str) -> User:
    """Resolve or provision a wallet user through the user repository."""
    return UserRepository(db).get_or_create_for_login(wallet_address)


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return UserRepository(db).get_by_id(user_id)


def find_refresh_token(db: Session, raw_refresh_token: str) -> RefreshToken | None:
    return find_active_refresh_token_record(db, raw_refresh_token=raw_refresh_token)


def persist_refresh_token(
    db: Session,
    *,
    user_id: int,
    raw_refresh_token: str,
    expires_at: datetime,
) -> RefreshToken:
    """Insert a refresh-token record and commit the login transaction."""
    record = create_refresh_token_record(
        db,
        user_id=user_id,
        raw_refresh_token=raw_refresh_token,
        expires_at=expires_at,
    )
    db.commit()
    return record


def rotate_refresh_token(
    db: Session,
    current_record: RefreshToken,
    *,
    user_id: int,
    raw_refresh_token: str,
    expires_at: datetime,
) -> RefreshToken:
    """Atomically revoke the current token and persist its replacement."""
    current_record.is_revoked = True
    replacement = create_refresh_token_record(
        db,
        user_id=user_id,
        raw_refresh_token=raw_refresh_token,
        expires_at=expires_at,
    )
    db.commit()
    return replacement


def revoke_refresh_token(db: Session, record: RefreshToken) -> None:
    """Revoke a refresh token and commit the logout transaction."""
    record.is_revoked = True
    db.commit()
