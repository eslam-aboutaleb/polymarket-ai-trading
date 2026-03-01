"""Centralized encrypted credential storage over key_store cache."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from cryptography.fernet import InvalidToken

from app.config import get_settings
from app.security.crypto import EncryptionConfigError, decrypt_text, encrypt_text
from app.utils.cache import key_store

logger = logging.getLogger(__name__)

CREDENTIAL_ENVELOPE_VERSION = 1


class WalletCredentials(TypedDict):
    private_key: str
    clob_creds: Optional[dict[str, str]]


class CredentialStoreError(RuntimeError):
    """Raised when credential storage is misconfigured or unavailable."""


_metrics = {
    "plaintext_migrated": 0,
    "decrypt_failed": 0,
}


def _cache_key(wallet_address: str) -> str:
    return f"pk:{wallet_address.lower()}"


def _normalize_clob_creds(clob_creds: Any) -> Optional[dict[str, str]]:
    if not isinstance(clob_creds, dict):
        return None
    normalized: dict[str, str] = {}
    for key in ("api_key", "api_secret", "api_passphrase"):
        value = clob_creds.get(key)
        if isinstance(value, str) and value:
            normalized[key] = value
    return normalized or None


def _encrypt_payload(private_key: str, clob_creds: Optional[dict[str, str]]) -> dict[str, Any]:
    private_key_enc, kid = encrypt_text(private_key)
    clob_creds_enc: Optional[dict[str, str]] = None
    if clob_creds:
        clob_creds_enc = {}
        for key, value in clob_creds.items():
            token, _ = encrypt_text(value)
            clob_creds_enc[key] = token

    return {
        "v": CREDENTIAL_ENVELOPE_VERSION,
        "kid": kid,
        "private_key_enc": private_key_enc,
        "clob_creds_enc": clob_creds_enc,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


def _decrypt_payload(payload: dict[str, Any]) -> WalletCredentials:
    private_key_enc = payload.get("private_key_enc")
    if not isinstance(private_key_enc, str) or not private_key_enc:
        raise InvalidToken("Missing encrypted private key field.")

    private_key = decrypt_text(private_key_enc)
    clob_raw = payload.get("clob_creds_enc")
    clob_creds: Optional[dict[str, str]] = None
    if isinstance(clob_raw, dict):
        clob_creds = {}
        for key, value in clob_raw.items():
            if isinstance(value, str) and value:
                clob_creds[key] = decrypt_text(value)
        if not clob_creds:
            clob_creds = None

    return {"private_key": private_key, "clob_creds": clob_creds}


def _is_encrypted_payload(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("v") == CREDENTIAL_ENVELOPE_VERSION
        and "private_key_enc" in payload
    )


def _is_legacy_plaintext_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("private_key"), str)


def store_wallet_credentials(
    wallet_address: str,
    private_key: str,
    clob_creds: Optional[dict[str, str]] = None,
    ttl_seconds: Optional[int] = None,
) -> None:
    """Encrypt and store wallet credentials in cache."""
    settings = get_settings()
    if not private_key:
        raise CredentialStoreError("Private key is required for credential storage.")
    ttl = ttl_seconds if ttl_seconds is not None else settings.credential_cache_ttl_seconds
    try:
        payload = _encrypt_payload(private_key, _normalize_clob_creds(clob_creds))
    except EncryptionConfigError as exc:
        raise CredentialStoreError(str(exc)) from exc
    key_store.set(_cache_key(wallet_address), payload, ttl_seconds=ttl)


def load_wallet_credentials(wallet_address: str) -> Optional[WalletCredentials]:
    """Load wallet credentials and migrate legacy plaintext entries."""
    key = _cache_key(wallet_address)
    stored = key_store.get(key)
    if stored is None:
        return None

    # New encrypted envelope.
    if _is_encrypted_payload(stored):
        try:
            return _decrypt_payload(stored)
        except EncryptionConfigError as exc:
            raise CredentialStoreError(str(exc)) from exc
        except InvalidToken:
            _metrics["decrypt_failed"] += 1
            key_store.delete(key)
            logger.warning("Deleted undecryptable credential entry for wallet=%s", wallet_address)
            return None

    # Legacy plaintext payload. Migrate and return once.
    if _is_legacy_plaintext_payload(stored):
        plaintext: WalletCredentials = {
            "private_key": stored["private_key"],
            "clob_creds": _normalize_clob_creds(stored.get("clob_creds")),
        }
        try:
            store_wallet_credentials(
                wallet_address=wallet_address,
                private_key=plaintext["private_key"],
                clob_creds=plaintext["clob_creds"],
            )
            _metrics["plaintext_migrated"] += 1
            logger.info("Migrated plaintext credential cache entry for wallet=%s", wallet_address)
        except CredentialStoreError:
            # Fail closed: do not retain plaintext when key configuration is invalid.
            key_store.delete(key)
            raise
        return plaintext

    # Unknown payload shape: remove it to avoid repeated parsing failures.
    key_store.delete(key)
    logger.warning("Deleted invalid credential cache payload for wallet=%s", wallet_address)
    return None


def delete_wallet_credentials(wallet_address: str) -> bool:
    """Delete cached wallet credentials."""
    return key_store.delete(_cache_key(wallet_address))


def get_credential_store_metrics() -> dict[str, int]:
    return dict(_metrics)
