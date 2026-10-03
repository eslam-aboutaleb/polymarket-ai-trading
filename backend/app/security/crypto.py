"""Credential envelope encryption with pluggable backends and key rotation."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings

logger = logging.getLogger(__name__)


class EncryptionConfigError(ValueError):
    """Raised when encryption configuration is missing or malformed."""


class EnvelopeEncryptor(Protocol):
    """Provider-agnostic envelope encryption interface.

    Implementations encrypt plaintext and return (ciphertext, key_id);
    consumers never depend on a specific KMS provider or library.
    """

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        """Encrypt plaintext and return (ciphertext, key id)."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext produced by encrypt()."""
        ...


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


@lru_cache
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

    parts: list[str] = [p.strip() for p in raw.split(",") if p.strip()]
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


class FernetEnvelopeEncryptor:
    """Local envelope encryptor backed by the rotating Fernet keyring."""

    def __init__(self, keyring: FernetKeyring | None = None) -> None:
        self._keyring = keyring if keyring is not None else get_fernet_keyring()

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        token = self._keyring.primary.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return token, self._keyring.primary_kid

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._keyring.decryptor.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise InvalidToken("Credential decryption failed.") from exc


class GcpKmsEnvelopeEncryptor:
    """Envelope encryptor using GCP Cloud KMS to wrap per-value data keys.

    Each value is encrypted locally with a one-time Fernet data key; the data
    key itself is wrapped by GCP KMS. Only the wrapped data key and the local
    ciphertext are persisted, so plaintext never leaves the process and KMS key
    material never leaves Google. The KMS client is imported lazily so the
    google-cloud-kms package is only required when this backend is selected.
    """

    def __init__(self, key_name: str, client_factory: Callable[[], object] | None = None) -> None:
        if not key_name:
            raise EncryptionConfigError(
                "GCP_KMS_KEY_NAME must be configured for the gcp_kms backend."
            )
        self._key_name = key_name
        self._client_factory = client_factory
        self._client = None

    def _get_client(self) -> object:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                try:
                    from google.cloud import kms
                except ImportError as exc:
                    raise EncryptionConfigError(
                        "The gcp_kms credential encryption backend requires the "
                        "google-cloud-kms package."
                    ) from exc
                self._client = kms.KeyManagementServiceClient()
        return self._client

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        data_key = os.urandom(32)
        fernet = Fernet(base64.urlsafe_b64encode(data_key))
        token = fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        response = self._get_client().encrypt(name=self._key_name, plaintext=data_key)
        wrapped_key = base64.urlsafe_b64encode(response.ciphertext).decode("utf-8")
        return f"{wrapped_key}:{token}", self._key_id()

    def decrypt(self, ciphertext: str) -> str:
        wrapped_key, token = ciphertext.split(":", 1)
        response = self._get_client().decrypt(
            name=self._key_name,
            ciphertext=base64.urlsafe_b64decode(wrapped_key.encode("utf-8")),
        )
        fernet = Fernet(base64.urlsafe_b64encode(response.plaintext))
        try:
            return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise InvalidToken("Credential decryption failed.") from exc

    def _key_id(self) -> str:
        return hashlib.sha256(self._key_name.encode("utf-8")).hexdigest()[:12]


@lru_cache
def get_envelope_encryptor() -> EnvelopeEncryptor:
    """Build and cache the envelope encryptor selected by configuration."""
    settings = get_settings()
    backend = (settings.credential_encryption_backend or "fernet").strip().lower()
    if backend == "fernet":
        return FernetEnvelopeEncryptor()
    if backend == "gcp_kms":
        return GcpKmsEnvelopeEncryptor(key_name=settings.gcp_kms_key_name)
    raise EncryptionConfigError(
        f"Unknown CREDENTIAL_ENCRYPTION_BACKEND: {backend!r} (expected 'fernet' or 'gcp_kms')."
    )


def encrypt_text(value: str) -> tuple[str, str]:
    """Encrypt plaintext value and return ciphertext plus key id."""
    return get_envelope_encryptor().encrypt(value)


def decrypt_text(token: str) -> str:
    """Decrypt ciphertext using the configured envelope encryptor."""
    return get_envelope_encryptor().decrypt(token)
