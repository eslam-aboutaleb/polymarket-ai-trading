"""Account service — sub-account create/list/resolve (plan 06 Phase A).

Pure and deterministic: no network, no clock reads beyond the model
default, no caching. Ownership is the only rule that matters, and it is
enforced in exactly one place (``resolve_account``) so every caller gets
the same answer for the same inputs.

Invariants:

* Every user has exactly one ``personal`` account. It is created lazily
  on first resolution rather than assumed, so a database that predates
  the backfill (or a freshly seeded test user) still resolves.
* A ``personal`` account is the owner's default: ``is_default`` is
  flipped off for the owner's other accounts when a personal account is
  provisioned.
* A sub-account is never default, so a fresh login can never land in
  someone's segregated trading account by accident.
* An id that belongs to another user is refused (``AccountAccessDenied``)
  rather than silently falling through to the personal account — a
  crossed-account request is a bug or an attack, not a typo.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account, AccountKind
from app.models.account_settings import AccountSettings
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

PERSONAL_ACCOUNT_NAME = "Personal"

#: Settings fields an account's PATCH endpoint is allowed to touch.
#: Deliberately a whitelist rather than ``__dict__``: identity and
#: bookkeeping columns (peak_capital, consecutive_wins, trading_halted)
#: are owned by the trading engines that maintain them, and a settings
#: round-trip must not be able to clear a halt or invent a high-water
#: mark.
EDITABLE_SETTINGS_FIELDS = frozenset(
    {
        "ai_backend",
        "preferred_llm_provider",
        "preferred_llm_model",
        "copy_trading_enabled",
        "risk_mode",
        "max_position_size",
        "daily_loss_limit",
        "mirror_percentage",
        "fixed_trade_amount",
        "kelly_fraction",
        "require_ai_approval",
        "follow_email_notifications_enabled",
        "daily_digest_enabled",
        "monthly_loss_limit",
        "max_drawdown_pct",
        "total_loss_halt_pct",
        "dynamic_sizing_enabled",
        "simulation_mode",
        "paper_balance",
        "inverse_bot_enabled",
        "inverse_bot_default_size_mode",
        "inverse_bot_fixed_amount",
        "inverse_bot_confidence_threshold",
        "inverse_bot_cooldown_minutes",
        "inverse_bot_max_reversals_per_day",
    }
)


class AccountAccessDenied(Exception):
    """The requested account exists but belongs to a different user."""


def _personal_account(db: Session, user_id: int) -> Account | None:
    # Oldest wins if a database somehow holds two: the migration and
    # ``ensure_personal_account`` both guarantee exactly one, so this is
    # only a deterministic tie-break rather than a second code path.
    return (
        db.execute(
            select(Account)
            .where(
                Account.owner_user_id == user_id,
                Account.kind == AccountKind.PERSONAL.value,
            )
            .order_by(Account.id)
            .limit(1)
        )
        .scalars()
        .first()
    )


def _demote_other_defaults(db: Session, user_id: int, keep_id: int) -> None:
    """Keep exactly one default account per owner."""
    others = (
        db.execute(
            select(Account).where(
                Account.owner_user_id == user_id,
                Account.is_default.is_(True),
                Account.id != keep_id,
            )
        )
        .scalars()
        .all()
    )
    for other in others:
        other.is_default = False
    if others:
        db.flush()


def ensure_personal_account(db: Session, user_id: int) -> Account:
    """Return the user's personal account, creating it when absent.

    The creation is idempotent under a unique-constraint race: a
    concurrent resolver that won the insert makes this one re-read the
    row instead of failing. Only the ``accounts`` row is written here —
    pointing existing rows at it is the migration's job, so a database
    that predates the backfill gains its account without its rows being
    silently re-mapped.
    """
    existing = _personal_account(db, user_id)
    if existing is not None:
        if not existing.is_default:
            existing.is_default = True
            _demote_other_defaults(db, user_id, existing.id)
            db.commit()
            db.refresh(existing)
        return existing

    account = Account(
        owner_user_id=user_id,
        name=PERSONAL_ACCOUNT_NAME,
        kind=AccountKind.PERSONAL.value,
        is_default=True,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with another request creating the same account.
        db.rollback()
        existing = _personal_account(db, user_id)
        if existing is None:
            raise
        return existing
    db.refresh(account)
    logger.info("Provisioned personal account %s for user_id=%s", account.id, user_id)
    return account


def create_sub_account(db: Session, owner_user_id: int, name: str) -> Account:
    """Create a segregated sub-account under an existing owner.

    The owner must already have a personal account — that is the
    invariant that keeps "one owner, one login wallet" true while
    allowing many trading boundaries. Creating the personal row here
    would hide a database that never got its backfill.
    """
    if _personal_account(db, owner_user_id) is None:
        raise ValueError(f"user {owner_user_id} has no personal account")

    account = Account(
        owner_user_id=owner_user_id,
        name=name,
        kind=AccountKind.SUB.value,
        is_default=False,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info(
        "Created sub-account %s (%r) for user_id=%s",
        account.id,
        name,
        owner_user_id,
    )
    return account


def list_accounts(db: Session, user_id: int) -> list[Account]:
    """List an owner's accounts, personal first then oldest sub-account."""
    accounts = (
        db.execute(
            select(Account)
            .where(Account.owner_user_id == user_id)
            .order_by(
                Account.is_default.desc(),
                Account.created_at.asc(),
                Account.id.asc(),
            )
        )
        .scalars()
        .all()
    )
    if not any(account.kind == AccountKind.PERSONAL.value for account in accounts):
        personal = ensure_personal_account(db, user_id)
        accounts.insert(0, personal)
    return accounts


def resolve_account(db: Session, user_id: int, account_id: int | str | None) -> Account:
    """Resolve the account a request should operate on.

    ``None``, an empty string or an unparseable id fall back to the
    personal account: an absent header must behave exactly like today's
    user-scoped reads. An id with no row falls back the same way, which
    keeps the endpoint from confirming that a given account exists. An
    id owned by somebody else is refused.
    """
    if account_id is None or account_id == "":
        return ensure_personal_account(db, user_id)

    try:
        resolved_id = int(account_id)
    except (TypeError, ValueError):
        return ensure_personal_account(db, user_id)

    account = db.get(Account, resolved_id)
    if account is None:
        return ensure_personal_account(db, user_id)
    if account.owner_user_id != user_id:
        raise AccountAccessDenied(f"account {resolved_id} is not owned by user {user_id}")
    return account


def get_settings_row(db: Session, account: Account) -> AccountSettings | UserSettings:
    """Return the settings row that owns ``account``'s preferences.

    The personal account keeps using ``user_settings``: it is the row
    every pre-plan route reads, and duplicating it would create two
    sources of truth for the same account. A sub-account gets its own
    ``account_settings`` row, provisioned on first read so a client can
    PATCH it without a separate provisioning call.
    """
    if account.kind == AccountKind.PERSONAL.value:
        settings = (
            db.query(UserSettings).filter(UserSettings.user_id == account.owner_user_id).first()
        )
        if settings is None:
            settings = UserSettings(user_id=account.owner_user_id)
            db.add(settings)
            db.commit()
            db.refresh(settings)
        return settings

    return _ensure_account_settings(db, account.id)


def _ensure_account_settings(db: Session, account_id: int) -> AccountSettings:
    settings = db.query(AccountSettings).filter(AccountSettings.account_id == account_id).first()
    if settings is None:
        settings = AccountSettings(account_id=account_id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def apply_settings_updates(
    settings: AccountSettings | UserSettings,
    updates: dict,
) -> AccountSettings | UserSettings:
    """Write the whitelisted fields of ``updates`` onto ``settings``.

    Unknown keys are ignored rather than rejected so a newer frontend
    cannot 400 an older backend; ``EDITABLE_SETTINGS_FIELDS`` is what
    actually bounds the write.
    """
    for field, value in updates.items():
        if field in EDITABLE_SETTINGS_FIELDS and value is not None:
            setattr(settings, field, value)
    return settings
