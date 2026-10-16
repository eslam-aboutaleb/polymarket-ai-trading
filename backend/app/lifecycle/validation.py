"""Startup checks and one-time application initialization."""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from app.config import Settings
from app.security.crypto import EncryptionConfigError

logger = logging.getLogger(__name__)


def validate_startup_security(
    settings: Settings,
    get_keyring: Callable[[], Any],
) -> None:
    """Fail fast on security-critical misconfiguration."""
    # JWT strength is validated in Settings; this call keeps startup intent explicit.
    _ = settings.jwt_secret_key
    try:
        get_keyring()
    except EncryptionConfigError as exc:
        # Keep the API available in local/dev even if encrypted credential
        # storage isn't configured yet. Private-key login/trading paths still
        # fail closed via credential_store guards.
        if settings.is_local_environment:
            logger.warning(
                "Credential encryption not configured; private-key credential "
                "features are disabled until ENCRYPTION_MASTER_KEYS is set."
            )
            return
        raise RuntimeError(
            "Credential encryption is not configured. "
            "Set ENCRYPTION_MASTER_KEYS to one or more Fernet keys."
        ) from exc


def validate_database_connectivity(check_connectivity: Callable[[], None]) -> None:
    """Fail fast when the application database is unavailable."""
    try:
        check_connectivity()
    except Exception as exc:
        logger.error("Database connectivity validation failed: %s", exc)
        raise RuntimeError("Database connection failed during startup.") from exc


def sync_admin_wallets(
    settings: Settings,
    session_scope: Callable[[], AbstractContextManager[Any]],
) -> None:
    """Promote or demote users based on the configured admin wallets."""
    try:
        from app.services.users.admin_sync_service import sync_admin_wallets as sync

        with session_scope() as db:
            sync(
                db,
                settings.admin_wallets,
                allow_empty=settings.admin_sync_allow_empty,
            )
    except Exception:
        logger.exception("Failed to sync admin wallets")
        raise
