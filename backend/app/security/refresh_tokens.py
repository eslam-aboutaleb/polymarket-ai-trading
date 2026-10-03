"""Refresh-token hashing and persistence helpers."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.token import RefreshToken


def _refresh_token_hash_secret() -> str:
    settings = get_settings()
    # Dedicated key is preferred; fallback keeps rollout backward-compatible.
    secret = settings.refresh_token_hash_secret.strip() or settings.jwt_secret_key.strip()
    if not secret:
        raise ValueError("Refresh token hash secret is not configured.")
    return secret


def hash_refresh_token(raw_token: str) -> str:
    """Return deterministic HMAC-SHA256 digest for a refresh token."""
    token = (raw_token or "").strip()
    if not token:
        raise ValueError("Refresh token cannot be empty.")
    return hmac.new(
        _refresh_token_hash_secret().encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_refresh_token_record(
    db: Session,
    *,
    user_id: int,
    raw_refresh_token: str,
    expires_at: datetime,
) -> RefreshToken:
    """Persist hashed refresh token."""
    record = RefreshToken(
        user_id=user_id,
        token_hash=hash_refresh_token(raw_refresh_token),
        expires_at=expires_at,
    )
    db.add(record)
    return record


def find_active_refresh_token_record(
    db: Session,
    *,
    raw_refresh_token: str,
) -> RefreshToken | None:
    """Find active refresh-token row by hash."""
    return (
        db.query(RefreshToken)
        .filter(
            RefreshToken.token_hash == hash_refresh_token(raw_refresh_token),
            RefreshToken.is_revoked == False,  # noqa: E712
        )
        .first()
    )
