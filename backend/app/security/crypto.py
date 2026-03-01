"""Credential encryption helpers with key rotation support."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings

logger = logging.getLogger(__name__)


class EncryptionConfigError(ValueError):
    """Raised when ENCRYPTION_MASTER_KEYS is missing or malformed."""


@dataclass(frozen=True)
class FernetKeyring:
    """Holds ordered Fernet keys for encrypt/decrypt operations."""

    primary: Fernet
    decryptor: MultiFernet
    primary_kid: str


def _validate_fernet_key(raw_key: str) -> None:
    try:
        decoded = base64.urlsafe_b64decode(raw_key.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - guardrail
        raise EncryptionConfigError("Invalid Fernet key encoding.") from exc
    if len(decoded) != 32:
        raise EncryptionConfigError("Each Fernet key must decode to exactly 32 bytes.")
    try:
        Fernet(raw_key.encode("utf-8"))
    except Exception as exc:  # pragma: no cover - guardrail
        raise EncryptionConfigError("Invalid Fernet key material.") from exc


def _key_id(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]


def _derive_local_fallback_key(material: str) -> str:
    """Derive a deterministic Fernet key from local dev material."""
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


@lru_cache()
def get_fernet_keyring() -> FernetKeyring:
    """Build and cache a keyring from ENCRYPTION_MASTER_KEYS."""
    settings = get_settings()
    raw = settings.encryption_master_keys.strip()
    if not raw:
        # Backward-compatible fallback during rollout from singular key variable.
        raw = os.environ.get("ENCRYPTION_MASTER_KEY", "").strip()
    if not raw:
        if settings.is_local_environment:
            fallback = _derive_local_fallback_key(settings.jwt_secret_key)
            logger.warning(
                "ENCRYPTION_MASTER_KEYS is not set; using local fallback key "
                "derived from JWT_SECRET_KEY (development only)."
            )
            raw = fallback
        else:
            raise EncryptionConfigError("ENCRYPTION_MASTER_KEYS must be configured.")

    parts: List[str] = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise EncryptionConfigError("ENCRYPTION_MASTER_KEYS is empty.")

    for key in parts:
        _validate_fernet_key(key)

    fernets = [Fernet(key.encode("utf-8")) for key in parts]
    return FernetKeyring(
        primary=fernets[0],
        decryptor=MultiFernet(fernets),
        primary_kid=_key_id(parts[0]),
    )


def encrypt_text(value: str) -> tuple[str, str]:
    """Encrypt plaintext value and return ciphertext plus key id."""
    keyring = get_fernet_keyring()
    token = keyring.primary.encrypt(value.encode("utf-8")).decode("utf-8")
    return token, keyring.primary_kid


def decrypt_text(token: str) -> str:
    """Decrypt ciphertext using all configured keys."""
    keyring = get_fernet_keyring()
    try:
        return keyring.decryptor.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise InvalidToken("Credential decryption failed.") from exc
