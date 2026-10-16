"""Synchronize configured administrator wallets with persisted user roles."""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.repositories.users.user_repository import UserRepository

logger = logging.getLogger(__name__)
_WALLET_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")


def sync_admin_wallets(
    db: Session,
    configured_wallets: str,
    *,
    allow_empty: bool = False,
) -> None:
    """Apply the administrator wallet allowlist in the caller's transaction.

    ``allow_empty`` is an explicit guard against mass demotion when an
    operator clears or forgets the environment variable. The query and audit
    writes are delegated to repositories; this service owns the role policy.
    """
    raw = configured_wallets.strip()
    if not raw and not allow_empty:
        logger.warning(
            "ADMIN_WALLETS is empty and ADMIN_SYNC_ALLOW_EMPTY=false; "
            "skipping admin sync to prevent destructive demotion."
        )
        return

    admin_wallets: set[str] = set()
    for candidate in (item.strip() for item in raw.split(",")):
        if not candidate:
            continue
        if not _WALLET_PATTERN.fullmatch(candidate):
            logger.warning("Ignoring invalid admin wallet in ADMIN_WALLETS: %s", candidate)
            continue
        admin_wallets.add(candidate.lower())

    from app.models.platform.audit_log import AdminAuditLog
    from app.models.users.user import User

    users = db.query(User).all() if hasattr(db, "query") else UserRepository(db).list_all()
    for user in users:
        should_be_admin = user.wallet_address.lower() in admin_wallets
        if user.is_admin == should_be_admin:
            continue

        previous_state = user.is_admin
        user.is_admin = should_be_admin
        db.add(
            AdminAuditLog(
                wallet_address=user.wallet_address,
                action="promoted" if should_be_admin else "demoted",
                source="env_sync",
                previous_state=previous_state,
                new_state=should_be_admin,
            )
        )
        logger.warning(
            "ADMIN_CHANGE: %s %s (was=%s now=%s)",
            "PROMOTED" if should_be_admin else "DEMOTED",
            user.wallet_address,
            previous_state,
            should_be_admin,
        )
