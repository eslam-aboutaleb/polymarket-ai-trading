"""Shared FastAPI dependencies that are not route handlers.

``get_active_account`` is the account-context seam (plan 06 Phase A). It
reads the ``X-Account-Id`` header a client sends after switching
accounts in the UI, verifies ownership, and falls back to the caller's
personal account when the header is absent, empty or unknown. That
fallback is the whole rollout strategy: with
``accounts_enabled=False`` (the default) every existing route keeps
behaving exactly as it does today, and flipping the flag on does not
change the response for a client that never sends the header.

Ownership is verified here, once, rather than inside each scoped route:
a route that forgets the check is a cross-account data leak, and the
only reliable defence is a dependency every scoped route must use.

It lives in ``api/deps.py`` rather than a route module so that
``api/routes/accounts.py`` can import it without the import cycle that
importing a route module would create.

The same argument puts the plan gates here. ``require_plan(capability)``
is the sibling of ``require_admin`` in ``routes/settings.py``: it takes a
capability, never a plan, and every decision comes from
``EntitlementService`` so there is exactly one place that knows what a
tier includes. ``require_api_key`` is reused unchanged for the external
namespace's variant.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import Depends, Header, HTTPException, Request, status
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.api.routes.auth import (
    get_current_user_from_token,
    get_optional_user_from_token,
)
from app.config import get_settings
from app.models.account import Account, AccountKind
from app.services.account_service import (
    AccountAccessDenied,
    ensure_personal_account,
    resolve_account,
)
from app.services.api_key_service import (
    KEY_PREFIX_LENGTH,
    SCOPE_READ,
    SCOPE_TRADE,
    AuthContext,
    authenticate_api_key,
)
from app.services.entitlement_service import EntitlementService
from app.utils.database import get_db

logger = logging.getLogger(__name__)

#: Header the external API presents. Deliberately not a cookie and not a
#: bearer token: an API key is a machine credential, and letting it ride
#: the browser session would make every CSRF surface an external surface.
API_KEY_HEADER = "X-API-Key"

# Header the frontend sets once the user picks an account. Mirrors the
# plan's ``X-Account-Id`` name; a query-string ``?account_id=`` is
# deliberately not supported because it leaks the account into logs,
# referrers and browser history.
ACCOUNT_HEADER = "X-Account-Id"


def get_active_account(
    x_account_id: str | None = Header(default=None, alias=ACCOUNT_HEADER),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
) -> Account:
    """Resolve the account the caller is acting as.

    Returns the account named by ``X-Account-Id`` after proving the
    caller owns it. When the header is absent, empty, unparseable or
    names an account that does not exist, the caller's personal
    account is returned instead — the pre-plan behaviour.

    Raises:
        HTTPException: 401 when the request is unauthenticated (raised
            by ``get_current_user_from_token``), or 403 when the named
            account belongs to a different user.
    """
    if not get_settings().accounts_enabled:
        # Feature flag off: the account context is inert. The personal
        # account is still returned so a caller that reads
        # ``account.id`` for logging works unchanged.
        return ensure_personal_account(db, current_user["user_id"])

    try:
        return resolve_account(db, current_user["user_id"], x_account_id)
    except AccountAccessDenied as exc:
        logger.warning(
            "Rejected account switch: user_id=%s requested=%s",
            current_user.get("user_id"),
            x_account_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account not found or not owned by caller",
        ) from exc


def _resolve_gate_account(db: Session, user_id: int | None, x_account_id: str | None) -> Account:
    """Resolve the active account for a gated dependency, 403 on a foreign id.

    ``get_active_account`` converts ``AccountAccessDenied`` into a 403, but
    the plan/quota gate dependencies resolve the account directly (only when
    entitlement enforcement is on). Sharing that guard keeps the two paths
    returning the same 403 instead of one leaking a 500.
    """
    try:
        return resolve_account(db, user_id, x_account_id)
    except AccountAccessDenied as exc:
        logger.warning(
            "Rejected account switch in gate: user_id=%s requested=%s",
            user_id,
            x_account_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account not found or not owned by caller",
        ) from exc


def external_api_rate_limit_key(request: Request) -> str:
    """SlowAPI key function for the external namespace.

    Requests that present an API key are limited per key (by its public
    prefix), everything else per IP. That keeps the two limit pools
    separate: a frontend behind one NAT cannot exhaust a key's budget,
    and a key cannot borrow the anonymous pool's headroom.

    Only the prefix is used, so the raw key never reaches the limiter's
    storage (which, with Redis configured, is a second process).
    """
    raw_key = (request.headers.get(API_KEY_HEADER) or "").strip()
    if raw_key:
        return f"api_key:{raw_key[:KEY_PREFIX_LENGTH]}"
    return f"ip:{get_remote_address(request)}"


def require_api_key(scope: str = SCOPE_READ) -> Callable[..., AuthContext]:
    """Build the dependency that authenticates an external request.

    ``scope`` is the minimum capability the route needs: ``read`` for
    everything except ``POST /orders``, which requires ``trade``. The
    distinction is deliberate — a wrong scope is a 403 (the key is real,
    it is just not allowed here), while a bad or absent key is a 401
    (the caller is unknown).

    There is no ``User`` session here at all: no cookie, no bearer
    token, no admin claim. An API key is the same owner acting through a
    channel that cannot hold a cookie, and ``AuthContext`` is the only
    shape a route receives.
    """

    def dependency(
        x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
        db: Session = Depends(get_db),
    ) -> AuthContext:
        if not x_api_key or not x_api_key.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-API-Key header",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        try:
            context = authenticate_api_key(db, x_api_key)
        except ValueError as exc:
            # The hash secret is not configured: authentication cannot be
            # performed at all, so fail closed and loudly rather than
            # treating a misconfigured server as a bad key.
            logger.error("API key authentication unavailable: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API key authentication is not configured",
            ) from exc
        if context is None:
            # One message for unknown, revoked and expired keys so the
            # endpoint cannot be used to probe which prefixes exist.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if not context.has_scope(scope):
            logger.info(
                "API key %s lacks scope %s (has %s)",
                context.key_prefix,
                scope,
                sorted(context.scopes),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key requires the '{scope}' scope",
            )
        return context

    return dependency


# ── Plan / entitlement gates (plan 09 Phase A) ──────────────────
#
# The gate is a sibling of ``require_api_key``, not a new idea: it takes a
# capability name, never a plan name, and delegates every decision to
# ``EntitlementService``. A route that writes ``if plan == "pro"`` is
# therefore always wrong, and adding a capability to the matrix needs no
# route edit at all.
#
# The gated route keeps working with the dependency present and
# enforcement switched off (``entitlements_enabled``, default false per
# master plan I12), which is what makes this rollout additive: the matrix
# ships reviewed, and the switch decides when a request is first refused.


def require_plan(capability: str) -> Callable[..., Account | None]:
    """Build the dependency that requires ``capability`` of the active account.

    Raises:
        HTTPException: 402 with a structured body naming the required plan
            when the account's tier does not include ``capability``, 403
            when the capability is not one any plan grants, and 401 from
            ``get_current_user_from_token`` when unauthenticated.

    The account is resolved **lazily**, only when enforcement is on. That
    is not an optimisation: with ``entitlements_enabled`` false (the
    default) the gate is inert, and resolving an account would put a
    database round-trip — and an ``ensure_personal_account`` write — on the
    path of every request to a gated route that never needed one. It also
    means the returned value is ``None`` unless enforcement is on, so a
    route must not depend on it for scoping; it is there for symmetry with
    ``require_api_key``.
    """

    def dependency(
        current_user: dict = Depends(get_current_user_from_token),
        x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
        db: Session = Depends(get_db),
    ) -> Account | None:
        service = EntitlementService(db)
        if not service.enforcement_enabled():
            return None
        account = _resolve_gate_account(db, current_user.get("user_id"), x_account_id)
        service.assert_capability(account, capability)
        return account

    return dependency


#: ``require_capability`` is the same dependency under the name the
#: entitlement service uses. Both spellings exist because call sites read
#: either way and a second implementation would be a second decision.
require_capability = require_plan


def require_plan_quota(capability: str) -> Callable[..., Account | None]:
    """Build the dependency that spends one unit of a per-plan quota.

    Separate from :func:`require_plan` because the two answer different
    questions: the capability gate is boolean ("is this surface on your
    plan?"), the quota is a budget ("have you used today's allowance?").
    A surface that is enabled but exhausted answers 429, not 402.
    """

    def dependency(
        current_user: dict = Depends(get_current_user_from_token),
        x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
        db: Session = Depends(get_db),
    ) -> Account | None:
        service = EntitlementService(db)
        if not service.enforcement_enabled():
            return None
        account = _resolve_gate_account(db, current_user.get("user_id"), x_account_id)
        service.consume_quota(account, capability)
        return account

    return dependency


def optional_plan_quota(capability: str) -> Callable[..., Account | None]:
    """Quota dependency for a route that also serves anonymous callers.

    Several analysis surfaces are public by design: a visitor with no
    session can ask for a market score, and gating them behind
    authentication would change what the product is. An anonymous request
    is therefore simply not metered — there is no account to charge and no
    budget to spend — while an authenticated one is charged against its
    plan exactly like any other caller.
    """

    def dependency(
        current_user: dict | None = Depends(get_optional_user_from_token),
        x_account_id: str | None = Header(default=None, alias="X-Account-Id"),
        db: Session = Depends(get_db),
    ) -> Account | None:
        service = EntitlementService(db)
        if not service.enforcement_enabled():
            return None
        if not current_user or not current_user.get("user_id"):
            return None
        account = _resolve_gate_account(db, current_user["user_id"], x_account_id)
        service.consume_quota(account, capability)
        return account

    return dependency


def require_external_plan(capability: str, scope: str = SCOPE_TRADE) -> Callable[..., AuthContext]:
    """Build the plan gate for the key-authenticated external namespace.

    ``/api/v1/external/*`` has no session, so the gate composes with
    ``require_api_key`` instead of ``get_active_account`` and checks the
    plan of the account the key is scoped to (or the owner's personal
    account when the key is unscoped). The 402 therefore arrives before
    the money path runs, and an external caller gets the same structured
    upgrade hint the UI does.
    """
    key_dependency = require_api_key(scope)

    def dependency(
        auth: AuthContext = Depends(key_dependency),
        db: Session = Depends(get_db),
    ) -> AuthContext:
        service = EntitlementService(db)
        if service.enforcement_enabled():
            service.assert_capability(_account_for_key(db, auth), capability)
        return auth

    return dependency


def _account_for_key(db: Session, auth: AuthContext) -> Account | None:
    """Resolve the account an API key acts as, without writing anything.

    Read-only on purpose: an external read must not provision a personal
    account as a side effect, and a key scoped to an account the owner no
    longer holds is refused rather than silently widened.
    """
    if auth.account_id is not None:
        account = db.get(Account, auth.account_id)
        if account is None or account.owner_user_id != auth.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key is scoped to an account the owner does not hold",
            )
        return account
    return (
        db.query(Account)
        .filter(
            Account.owner_user_id == auth.user_id,
            Account.kind == AccountKind.PERSONAL.value,
        )
        .order_by(Account.id)
        .first()
    )
