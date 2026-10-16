"""Account API — sub-account CRUD and per-account settings (plan 06 Phase A).

Six endpoints, all authenticated with the same cookie/JWT dependency
the rest of the internal API uses. Nothing here reaches the money path:
Phase A ends at "accounts exist, are switchable, and have their own
settings", so every other route keeps reading ``user_id`` exactly as
before and the ``X-Account-Id`` header simply resolves to the personal
account.

Route order matters: ``/active`` is declared before ``/{account_id}``
so a literal path is never swallowed by the path parameter.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import ACCOUNT_HEADER, get_active_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.users.account import Account
from app.models.users.account_settings import AccountSettings
from app.models.users.user_settings import UserSettings
from app.services.users.account_service import (
    create_sub_account,
    get_owned_account,
    get_settings_row,
    list_accounts,
    record_account_switch,
    rename_account,
    update_account_settings,
)
from app.services.users.entitlement_service import EntitlementService
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

MAX_SUB_ACCOUNT_NAME = 100


# ── Schemas ──────────────────────────────────────────────────────


class AccountResponse(BaseModel):
    """An account as the switcher and the settings page see it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_user_id: int
    name: str
    kind: str
    is_default: bool
    created_at: str | None = None


class AccountCreateRequest(BaseModel):
    """Create a segregated sub-account under the caller."""

    name: str = Field(..., min_length=1, max_length=MAX_SUB_ACCOUNT_NAME)


class AccountRenameRequest(BaseModel):
    """Rename an account the caller owns."""

    name: str = Field(..., min_length=1, max_length=MAX_SUB_ACCOUNT_NAME)


class AccountSettingsResponse(BaseModel):
    """Per-account settings.

    One model serves both ``user_settings`` (the personal account) and
    ``account_settings`` (sub-accounts) because the column set is
    deliberately identical — see ``app/models/account_settings.py``.
    """

    model_config = ConfigDict(from_attributes=True)

    account_id: int
    kind: str
    ai_backend: str
    preferred_llm_provider: str | None = None
    preferred_llm_model: str | None = None
    copy_trading_enabled: bool = False
    risk_mode: str = "max_position_daily_loss"
    max_position_size: float = 100.0
    daily_loss_limit: float = 500.0
    mirror_percentage: float = 10.0
    fixed_trade_amount: float = 50.0
    kelly_fraction: float = 0.25
    require_ai_approval: bool = True
    follow_email_notifications_enabled: bool = False
    daily_digest_enabled: bool = True
    monthly_loss_limit: float | None = None
    max_drawdown_pct: float = 25.0
    total_loss_halt_pct: float = 40.0
    dynamic_sizing_enabled: bool = False
    simulation_mode: bool = False
    paper_balance: float = 1000.0
    inverse_bot_enabled: bool = False
    inverse_bot_default_size_mode: str = "full_notional"
    inverse_bot_fixed_amount: float = 50.0
    inverse_bot_confidence_threshold: int = 75
    inverse_bot_cooldown_minutes: int = 30
    inverse_bot_max_reversals_per_day: int = 3
    updated_at: str | None = None


class AccountSettingsUpdateRequest(BaseModel):
    """Partial settings update; omitted fields are left untouched."""

    ai_backend: str | None = None
    preferred_llm_provider: str | None = None
    preferred_llm_model: str | None = None
    copy_trading_enabled: bool | None = None
    risk_mode: str | None = None
    max_position_size: float | None = Field(default=None, ge=0)
    daily_loss_limit: float | None = Field(default=None, ge=0)
    mirror_percentage: float | None = Field(default=None, ge=0, le=100)
    fixed_trade_amount: float | None = Field(default=None, ge=0)
    kelly_fraction: float | None = Field(default=None, ge=0, le=1)
    require_ai_approval: bool | None = None
    follow_email_notifications_enabled: bool | None = None
    daily_digest_enabled: bool | None = None
    monthly_loss_limit: float | None = Field(default=None, ge=0)
    max_drawdown_pct: float | None = None
    total_loss_halt_pct: float | None = None
    dynamic_sizing_enabled: bool | None = None
    simulation_mode: bool | None = None
    paper_balance: float | None = Field(default=None, ge=0)
    inverse_bot_enabled: bool | None = None
    inverse_bot_default_size_mode: str | None = None
    inverse_bot_fixed_amount: float | None = Field(default=None, ge=0)
    inverse_bot_confidence_threshold: int | None = Field(default=None, ge=0, le=100)
    inverse_bot_cooldown_minutes: int | None = Field(default=None, ge=0)
    inverse_bot_max_reversals_per_day: int | None = Field(default=None, ge=0)


class ActiveAccountResponse(BaseModel):
    """What the ``X-Account-Id`` header currently resolves to."""

    account: AccountResponse
    #: The feature flag. While false the header is ignored entirely.
    accounts_enabled: bool
    #: True only when the flag is on *and* a header was supplied, i.e.
    #: the response really reflects a client-side account switch rather
    #: than the personal-account fallback. The switcher uses this to
    #: tell "my selection is live" from "nothing was selected".
    header_used: bool


# ── Helpers ──────────────────────────────────────────────────────


def _account_response(account: Account) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        owner_user_id=account.owner_user_id,
        name=account.name,
        kind=account.kind,
        is_default=account.is_default,
        created_at=account.created_at.isoformat() if account.created_at else None,
    )


def _settings_response(
    account: Account,
    settings: AccountSettings | UserSettings,
) -> AccountSettingsResponse:
    # ``kind`` and ``account_id`` come from the account, not the settings
    # row, so they are excluded from the attribute copy.
    row_fields = set(AccountSettingsResponse.model_fields) - {"account_id", "kind"}
    payload = {field: getattr(settings, field) for field in row_fields}
    payload["account_id"] = account.id
    payload["kind"] = account.kind
    payload["updated_at"] = settings.updated_at.isoformat() if settings.updated_at else None
    return AccountSettingsResponse(**payload)


def _owned_account(db: Session, user_id: int, account_id: int) -> Account:
    """Fetch an account and prove the caller owns it.

    Raising 404 rather than 403 for a missing id keeps the endpoint
    from being an account-existence oracle; a foreign id gets the same
    404 so the response is indistinguishable.
    """
    account = get_owned_account(db, user_id, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )
    return account


# ── Routes ───────────────────────────────────────────────────────


@router.get("/active", response_model=ActiveAccountResponse)
def get_active_account_route(
    x_account_id: str | None = Header(default=None, alias=ACCOUNT_HEADER),
    current_user: dict = Depends(get_current_user_from_token),
    active_account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """Report which account the request's ``X-Account-Id`` header resolves to."""
    accounts_enabled = get_settings().accounts_enabled
    header_used = accounts_enabled and bool(x_account_id)
    # Emitted only for an explicit switch: the flag must be on and a
    # header must have been sent, which is exactly ``header_used``. A
    # client that never switches produces no audit row.
    if header_used:
        record_account_switch(
            db,
            user_id=current_user["user_id"],
            account_id=active_account.id,
        )
    return ActiveAccountResponse(
        account=_account_response(active_account),
        accounts_enabled=accounts_enabled,
        header_used=header_used,
    )


@router.get("", response_model=list[AccountResponse])
def list_accounts_route(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List the caller's accounts, personal account first."""
    accounts = list_accounts(db, current_user["user_id"])
    return [_account_response(account) for account in accounts]


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def create_account_route(
    request: AccountCreateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Create a segregated sub-account under the caller.

    The personal account must already exist: it is what makes the
    caller an owner rather than a wallet with no trading identity.
    """
    if not get_settings().accounts_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sub-accounts are disabled",
        )
    # Plan ceiling, checked after the feature flag: the flag decides
    # whether sub-accounts exist at all, the plan decides how many. The
    # free tier keeps exactly the one personal account it was backfilled
    # with, so an account that predates this check is never removed.
    EntitlementService(db).assert_account_creation_allowed(current_user["user_id"])
    try:
        account = create_sub_account(db, current_user["user_id"], request.name.strip())
    except ValueError as exc:
        logger.exception("Rejected sub-account creation for user_id=%s", current_user["user_id"])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sub-account could not be created",
        ) from exc
    # A new trading boundary is a security-relevant fact: it has its own
    # settings, its own loss limits and, once Phase B routes are scoped,
    # its own orders.
    return _account_response(account)


@router.patch("/{account_id}", response_model=AccountResponse)
def rename_account_route(
    account_id: int,
    request: AccountRenameRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Rename an account the caller owns."""
    account = _owned_account(db, current_user["user_id"], account_id)
    account = rename_account(db, account, request.name.strip())
    return _account_response(account)


@router.get("/{account_id}/settings", response_model=AccountSettingsResponse)
def get_account_settings_route(
    account_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Read one account's settings.

    The personal account reads ``user_settings`` (the row every existing
    route uses); a sub-account reads its own ``account_settings`` row.
    """
    account = _owned_account(db, current_user["user_id"], account_id)
    settings = get_settings_row(db, account)
    return _settings_response(account, settings)


@router.patch("/{account_id}/settings", response_model=AccountSettingsResponse)
def update_account_settings_route(
    account_id: int,
    request: AccountSettingsUpdateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Update one account's settings.

    Only the whitelisted fields in ``EDITABLE_SETTINGS_FIELDS`` are
    written, so a settings round-trip can never clear a trading halt or
    invent a high-water mark.
    """
    account = _owned_account(db, current_user["user_id"], account_id)
    settings = get_settings_row(db, account)
    requested = {
        key: value
        for key, value in request.model_dump(exclude_unset=True).items()
        if value is not None
    }
    # A settings write that switches a paid strategy on is refused here,
    # once, rather than in each strategy's own route. Turning a strategy
    # off is always allowed.
    EntitlementService(db).assert_settings_toggles(account, requested)
    settings = update_account_settings(
        db,
        user_id=current_user["user_id"],
        account=account,
        settings=settings,
        updates=requested,
    )
    return _settings_response(account, settings)
