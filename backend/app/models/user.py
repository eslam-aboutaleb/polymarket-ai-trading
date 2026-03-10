"""User model for authentication and wallet management"""
from sqlalchemy import Column, String, DateTime, Integer, Boolean, Text
from sqlalchemy.orm import validates
from datetime import datetime
from urllib.parse import urlparse

from app.utils.time import utc_now
from app.models.base import Base


def validate_profile_picture_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None

    parsed = urlparse(normalized)
    scheme = parsed.scheme.lower()

    if scheme in {"http", "https"}:
        if not parsed.netloc:
            raise ValueError("profile_picture_url must be an absolute http(s) URL")
        return normalized

    if scheme == "data":
        if normalized.lower().startswith("data:image/"):
            return normalized
        raise ValueError("profile_picture_url data URL must be image/*")

    raise ValueError("profile_picture_url must use http, https, or data:image/")


class User(Base):
    """User model with wallet-based authentication"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String(42), unique=True, nullable=False, index=True)  # Ethereum address
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)

    # ── Profile Fields ─────────────────────────────────────────
    display_name = Column(String(100), nullable=True)           # defaults to wallet address in API
    email = Column(String(255), nullable=True)                  # for 2FA
    phone = Column(String(30), nullable=True)                   # for 2FA
    profile_picture_url = Column(Text, nullable=True)           # base64 data-URL or external URL
    two_fa_enabled = Column(Boolean, default=False, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)     # admin flag — debug dashboard access

    @validates("profile_picture_url")
    def _validate_profile_picture_url(self, _key: str, value: str | None) -> str | None:
        return validate_profile_picture_url(value)
    
    def __repr__(self):
        return f"<User(id={self.id}, wallet={self.wallet_address})>"
