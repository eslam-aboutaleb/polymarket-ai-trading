"""External REST API, API-key management and account exposure (plan 06 Phase B).

Three surfaces, three different authentication regimes, and the split is
the point:

* ``/api/v1/external/*`` — machine clients. Authenticated *only* by an
  ``X-API-Key`` header (``api.deps.require_api_key``), rate-limited per
  key, and scope-gated: ``read`` everywhere except ``POST /orders``,
  which requires ``trade``. No cookie, no bearer token and no admin
  route is reachable from here, so the namespace is structurally
  incapable of touching credentials, settings or admin endpoints.
* ``/api/settings/api-keys/*`` — the *management* surface. Cookie/JWT
  authenticated like the rest of the internal API, because minting and
  revoking a credential is a browser action. It is deliberately outside
  the external namespace: an API key can never create or extend another
  API key, so a leaked key cannot be used to grant itself more access.
* ``/api/portfolio/exposure`` — the account-scoped exposure read. Same
  cookie/JWT plus ``X-Account-Id`` context as every other internal
  route.

Money-path parity: ``POST /orders`` is byte-for-byte the manual order
path in ``api/routes/trades.py`` — same idempotency helpers, same
position-cap pre-check, same ``apply_global_safety_caps`` call before
anything is signed, same ``_place_order_on_polymarket`` submission, and
``record_user_trade`` as the only ``UserTrade`` constructor (which also
appends the audit event). An external order is indistinguishable in
safety terms from a manual one.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    external_api_rate_limit_key,
    get_active_account,
    require_external_plan,
)
from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account, AccountKind
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.security.rate_limit import limiter
from app.services.analytics.execution_analytics import _FILLED_STATUSES
from app.services.market_data.polymarket_service import get_polymarket_service
from app.services.platform.audit_service import (
    DEFAULT_EXPORT_LIMIT,
    MAX_EXPORT_LIMIT,
    get_audit_events,
    normalize_event_types,
    parse_moment,
    to_csv,
)
from app.services.trading.copy_trade_service import _place_order_on_polymarket
from app.services.trading.external_order_service import (
    persist_external_order,
    persist_risk_gate_state,
)
from app.services.trading.external_trade_query_service import (
    list_account_trades,
    list_exposure_trades,
)
from app.services.trading.pre_trade_gate import (
    _to_float,
    apply_global_safety_caps,
    get_idempotent_order_result,
    store_idempotent_order_result,
)
from app.services.users.account_service import get_account_by_id, get_settings_row, resolve_account
from app.services.users.api_key_management_service import (
    create_api_key_with_audit,
    revoke_api_key_with_audit,
)
from app.services.users.api_key_service import (
    SCOPE_READ,
    SCOPE_TRADE,
    AuthContext,
    list_api_keys,
    serialize_api_key,
)
from app.services.users.entitlement_service import (
    CAP_COMPLIANCE_EXPORT,
    CAP_EXTERNAL_API_READ,
    CAP_EXTERNAL_API_TRADE,
)
from app.utils.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/external", tags=["external-api"])
api_keys_router = APIRouter(prefix="/api/settings/api-keys", tags=["api-keys"])
exposure_router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

#: Rows that represent live capital on an account. ``failed``,
#: ``rejected``, ``cancelled`` and ``simulated`` never held a position,
#: so counting them would overstate exposure.
OPEN_TRADE_STATUSES = ("pending", "executed", "filled")

# ── Rate limits ─────────────────────────────────────────────
# Applied as dependencies (the repo's idiom) so a route handler's own
# signature is untouched and tests can override them. Both use the
# per-key key function, so an external client's budget is tracked
# against its key rather than its IP or the shared anonymous pool.


@limiter.limit("120/minute", key_func=external_api_rate_limit_key)
def _external_read_rate_limit(request: Request) -> None:
    """Rate-limit external reads: 120/minute per API key (per IP if none)."""


@limiter.limit("30/minute", key_func=external_api_rate_limit_key)
def _external_write_rate_limit(request: Request) -> None:
    """Rate-limit external order submission: 30/minute per API key."""


@limiter.limit("10/minute")
def _api_key_create_rate_limit(request: Request) -> None:
    """Rate-limit key creation: 10/minute per IP (browser session)."""


# ── Schemas ─────────────────────────────────────────────────


class ExternalOrderRequest(BaseModel):
    """A limit order for the external API.

    Identical semantics to the manual execute request: ``size`` is USDC
    notional for a BUY and shares for a SELL, and the safety caps may
    reduce it (never raise it) before submission.
    """

    token_id: str = Field(..., min_length=1, description="Polymarket token ID (condition token)")
    market_id: str = Field(default="", description="Market slug or condition ID for record-keeping")
    side: str = Field(..., pattern="^(BUY|SELL)$", description="BUY or SELL")
    price: float = Field(..., gt=0, le=1, description="Limit price (0-1)")
    size: float = Field(
        ...,
        gt=0,
        description="BUY: amount in USDC, SELL: number of shares",
    )
    outcome: str = Field(default="", description="Yes/No outcome label")


class ExternalOrderResponse(BaseModel):
    """The order outcome, including the row the audit trail will show."""

    success: bool
    order_hash: str | None = None
    trade_id: int | None = None
    status: str
    side: str
    size: float
    price: float
    account_id: int | None = None
    strategy_source: str = "external"
    error: str | None = None


class ApiKeyCreateRequest(BaseModel):
    """Mint a key. The secret in the response is shown once and never again."""

    name: str = Field(default="", max_length=100, description="Human label for the key")
    account_id: int | None = Field(default=None, description="Scope the key to one account")
    scopes: list[str] = Field(default_factory=lambda: ["read"], description="Subset of read/trade")
    expires_in_days: int | None = Field(default=90, ge=1, le=365, description="Lifetime in days")


class ApiKeyResponse(BaseModel):
    """A key as the management UI sees it — never the secret, never a hash."""

    id: int
    name: str
    key_prefix: str
    account_id: int | None = None
    scopes: list[str] = []
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    created_at: str | None = None
    revoked: bool = False


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Creation response. ``secret`` exists only here, only once."""

    secret: str


class ExternalTradeRecord(BaseModel):
    """One order record as the external API reports it."""

    trade_id: int
    market_id: str
    token_id: str | None = None
    action: str
    side: str | None = None
    amount: float = 0.0
    price: float = 0.0
    status: str
    strategy_source: str | None = None
    order_hash: str | None = None
    created_at: str | None = None
    executed_at: str | None = None


class ExternalFillRecord(ExternalTradeRecord):
    """A fill: an order that actually settled on the exchange."""

    filled_price: float | None = None
    filled_size: float | None = None
    fee_paid: float | None = None
    slippage_bps: float | None = None
    latency_ms: float | None = None


class ExposureOutcome(BaseModel):
    """One outcome's exposure inside one market."""

    market_id: str
    token_id: str | None = None
    gross_notional: float = 0.0
    net_notional: float = 0.0
    gross_shares: float = 0.0
    net_shares: float = 0.0
    orders: int = 0


class ExposureResponse(BaseModel):
    """Per-market, per-outcome exposure for one trading account."""

    account_id: int
    account_name: str
    accounts_enabled: bool
    market_count: int = 0
    gross_notional: float = 0.0
    net_notional: float = 0.0
    outcomes: list[ExposureOutcome] = []
    limits: dict = {}


# ── Helpers ─────────────────────────────────────────────────


class _AccountSettingsAdapter:
    """A ``user_settings``-shaped view over a sub-account's settings row.

    ``apply_global_safety_caps`` was written against ``UserSettings``: it
    reads ``user_id`` for the loss aggregates and *writes* the halt,
    cooldown and peak-capital flags it derives. A sub-account must be
    able to be halted by its own breach, so the adapter delegates in
    both directions to the account row instead of copying it — a lost
    mutation would be a sub-account that keeps trading after its own
    drawdown gate fired.
    """

    def __init__(self, settings, user_id: int):
        object.__setattr__(self, "_settings", settings)
        object.__setattr__(self, "user_id", user_id)

    def __getattr__(self, name: str):
        return getattr(self._settings, name)

    def __setattr__(self, name: str, value):
        setattr(self._settings, name, value)


def _load_credentials(wallet_address: str) -> dict:
    """Load the user's trading credentials, or explain why they are absent.

    Mirrors the internal execute path exactly: a credential-store
    failure is a 503 (server problem, retry later) and a missing key is
    a 400 (the user has not completed the enable-auto-trading step).
    """
    try:
        stored = load_wallet_credentials(wallet_address)
    except CredentialStoreError as exc:
        # Never echo the raw store error to an API-key caller (internal-state
        # disclosure); log it server-side and return the same generic message
        # the internal order path uses.
        logger.exception("External API: credential store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Trading credentials are temporarily unavailable.",
        ) from exc
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No trading credentials found. Please re-login with your private key.",
        )
    return stored


def _settings_for_auth(db: Session, auth: AuthContext):
    """Resolve the settings row the safety caps must read.

    A key scoped to one account is gated by that account's limits, and
    is refused outright if that account no longer exists or is not the
    key owner's — silently widening a scoped key to the personal
    account would be the worst possible failure mode. An unscoped key
    uses the owner's personal account, which is the same settings row
    every internal route reads.
    """
    if auth.account_id is None:
        account = resolve_account(db, auth.user_id, None)
        # The personal account's settings row *is* a UserSettings row.
        return get_settings_row(db, account), account
    account = get_account_by_id(db, auth.account_id)
    if account is None or account.owner_user_id != auth.user_id:
        logger.warning(
            "API key %s is scoped to an unowned account %s",
            auth.key_prefix,
            auth.account_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is scoped to an account the owner does not hold",
        )
    settings = get_settings_row(db, account)
    return _AccountSettingsAdapter(settings, auth.user_id), account


def _order_response(
    *,
    result: dict,
    trade_size: float,
    price: float,
    side: str,
    trade_id: int | None,
    account_id: int | None,
) -> ExternalOrderResponse:
    success = bool(result.get("success"))
    return ExternalOrderResponse(
        success=success,
        order_hash=result.get("order_hash"),
        trade_id=trade_id,
        status="submitted" if success else "failed",
        side=side,
        size=trade_size,
        price=price,
        account_id=account_id,
        strategy_source="external",
        error=None if success else result.get("error"),
    )


def _auth_scoped_trades(
    db: Session, auth: AuthContext, statuses: tuple[str, ...], limit: int
) -> list[UserTrade]:
    """Orders for the key's owner, narrowed to the key's account when scoped."""
    if auth.account_id is not None:
        # A key scoped to an account the owner no longer holds is
        # refused rather than widened to the personal account.
        account = get_account_by_id(db, auth.account_id)
        if account is None or account.owner_user_id != auth.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key is scoped to an account the owner does not hold",
            )
    return list_account_trades(
        db,
        user_id=auth.user_id,
        account_id=auth.account_id,
        statuses=statuses,
        limit=limit,
    )


def _trade_record(row: UserTrade) -> ExternalTradeRecord:
    return ExternalTradeRecord(
        trade_id=row.id,
        market_id=row.market_id,
        token_id=row.token_id,
        action=row.action,
        side=(row.action or "").upper(),
        amount=float(row.amount or 0.0),
        price=float(row.price or 0.0),
        status=row.status,
        strategy_source=row.strategy_source,
        order_hash=row.order_hash,
        created_at=row.created_at.isoformat() if row.created_at else None,
        executed_at=row.executed_at.isoformat() if row.executed_at else None,
    )


def _fill_record(row: UserTrade) -> ExternalFillRecord:
    base = _trade_record(row)
    return ExternalFillRecord(
        **base.model_dump(),
        filled_price=row.filled_price,
        filled_size=row.filled_size,
        fee_paid=row.fee_paid,
        slippage_bps=row.slippage_bps,
        latency_ms=row.latency_ms,
    )


# ── External reads ──────────────────────────────────────────


@router.get(
    "/portfolio",
    dependencies=[Depends(_external_read_rate_limit)],
)
async def external_portfolio(
    auth: AuthContext = Depends(require_external_plan(CAP_EXTERNAL_API_READ, SCOPE_READ)),
):
    """Balance, positions and P&L for the key's owner.

    Read-only, and the credentials used are the owner's own: a key can
    read the wallet it belongs to and nothing else.
    """
    service = get_polymarket_service()
    stored = _load_credentials(auth.wallet_address)
    summary = await service.get_portfolio_summary(
        auth.wallet_address,
        private_key=stored["private_key"],
        clob_creds=stored.get("clob_creds"),
    )
    payload = dict(summary)
    payload["account_id"] = auth.account_id
    return payload


@router.get(
    "/positions",
    dependencies=[Depends(_external_read_rate_limit)],
)
async def external_positions(
    auth: AuthContext = Depends(require_external_plan(CAP_EXTERNAL_API_READ, SCOPE_READ)),
):
    """Open positions for the key's owner."""
    service = get_polymarket_service()
    stored = _load_credentials(auth.wallet_address)
    positions = await service.get_positions(
        auth.wallet_address,
        private_key=stored["private_key"],
        clob_creds=stored.get("clob_creds"),
    )
    return {"positions": positions, "count": len(positions), "account_id": auth.account_id}


@router.get(
    "/markets",
    dependencies=[Depends(_external_read_rate_limit)],
)
async def external_markets(
    limit: int = Query(default=25, ge=1, le=100),
    auth: AuthContext = Depends(require_external_plan(CAP_EXTERNAL_API_READ, SCOPE_READ)),
):
    """Trending active markets. Public market data, no wallet involved."""
    service = get_polymarket_service()
    markets = await service.get_active_markets(limit=limit)
    return {"markets": markets, "count": len(markets)}


@router.get(
    "/trades",
    response_model=list[ExternalTradeRecord],
    dependencies=[Depends(_external_read_rate_limit)],
)
async def external_trades(
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_external_plan(CAP_EXTERNAL_API_READ, SCOPE_READ)),
    db: Session = Depends(get_db),
):
    """Order records for the key's account, newest first.

    Sourced from the local ``user_trades`` rows rather than an exchange
    query so the external view is attributable (one row per strategy,
    with the order hash) and reads the same numbers as the audit trail.
    """
    rows = _auth_scoped_trades(db, auth, OPEN_TRADE_STATUSES, limit)
    return [_trade_record(row) for row in rows]


@router.get(
    "/fills",
    response_model=list[ExternalFillRecord],
    dependencies=[Depends(_external_read_rate_limit)],
)
async def external_fills(
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_external_plan(CAP_EXTERNAL_API_READ, SCOPE_READ)),
    db: Session = Depends(get_db),
):
    """Observed fills: submitted orders that settled, with slippage and fees."""
    rows = _auth_scoped_trades(db, auth, _FILLED_STATUSES, limit)
    return [_fill_record(row) for row in rows]


@router.get(
    "/audit",
    dependencies=[Depends(_external_read_rate_limit)],
)
async def external_audit(
    request: Request,
    # ``from``/``to`` are context-sensitive spellings, so the wire names are
    # bound through aliases; the parameter names stay legal identifiers.
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    type: str | None = None,
    aggregate_type: str | None = None,
    format: str = Query(default="json", pattern="^(json|csv)$"),
    limit: int = Query(default=DEFAULT_EXPORT_LIMIT, ge=1, le=MAX_EXPORT_LIMIT),
    auth: AuthContext = Depends(require_external_plan(CAP_COMPLIANCE_EXPORT, SCOPE_READ)),
    db: Session = Depends(get_db),
):
    """Compliance export of the owner's audit trail.

    Reads ``app_events`` through ``audit_service`` (which itself uses
    ``event_log.get_events``) — the append-only log plan 10 Phase A
    created, not a second audit table. ``?format=json`` returns the
    structured envelope; ``?format=csv`` returns the same rows as a
    spreadsheet-ready CSV.
    """
    since = parse_moment(from_)
    until = parse_moment(to)
    event_types = normalize_event_types(type)
    rows = get_audit_events(
        db,
        user_id=auth.user_id,
        account_id=auth.account_id,
        since=since,
        until=until,
        event_types=event_types,
        aggregate_type=aggregate_type,
        limit=limit,
    )
    if format == "csv":
        return PlainTextResponse(
            to_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="audit-events.csv"'},
        )
    return {
        "events": rows,
        "count": len(rows),
        "filters": {
            "from": since.isoformat() if since else None,
            "to": until.isoformat() if until else None,
            "types": event_types,
            "aggregate_type": aggregate_type,
            "account_id": auth.account_id,
            "limit": limit,
        },
    }


# ── External write ──────────────────────────────────────────


@router.post(
    "/orders",
    response_model=ExternalOrderResponse,
    dependencies=[Depends(_external_write_rate_limit)],
)
async def external_create_order(
    body: ExternalOrderRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auth: AuthContext = Depends(require_external_plan(CAP_EXTERNAL_API_TRADE, SCOPE_TRADE)),
    db: Session = Depends(get_db),
):
    """Place a limit order on Polymarket through the manual-order path.

    Safety parity with ``POST /api/trades/execute`` is the contract, in
    this order: idempotent replay first, then the credentials, then the
    position-cap pre-check, then ``apply_global_safety_caps`` *before*
    anything is signed, then ``_place_order_on_polymarket``, then
    ``record_user_trade`` — which is the only sanctioned ``UserTrade``
    constructor and also emits the ``order_*`` audit event, so the
    external order appears in the audit trail exactly like a manual one
    and is never double-logged.
    """
    # Idempotency: a replayed key returns the stored result, so a
    # retried request can never place a second order.
    # Namespace the replay key by the key's account so a multi-account
    # owner reusing the same Idempotency-Key on a second account cannot
    # collide with the first account's stored result. An unscoped key
    # (personal account) keeps the original key.
    replay_key = idempotency_key
    if idempotency_key and auth.account_id is not None:
        replay_key = f"acct{auth.account_id}:{idempotency_key}"
    if idempotency_key:
        prior = get_idempotent_order_result(db, auth.user_id, replay_key)
        if prior is not None:
            logger.info(
                "Replaying idempotent external order result for user_id=%s key=%s",
                auth.user_id,
                replay_key[:16],
            )
            return ExternalOrderResponse(**prior)

    stored = _load_credentials(auth.wallet_address)
    settings, account = _settings_for_auth(db, auth)

    # Position-cap pre-check, identical to the manual path: an over-cap
    # order is rejected, naming the violated limit.
    max_position_size = _to_float(settings.max_position_size)
    if max_position_size > 0 and body.size > max_position_size:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="max_position_size",
        )

    # Global safety caps — halt, cooldown, loss limits, drawdown and
    # position size counting pending exposure — run before signing.
    trade_size, rejection = apply_global_safety_caps(
        settings, body.size, db, account_id=auth.account_id
    )
    if rejection:
        # Persist any halt / drawdown flags the gate set.
        persist_risk_gate_state(db)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=rejection)

    logger.info(
        "External order via key %s: %s token=%s price=%.4f size=%.4f account=%s",
        auth.key_prefix,
        body.side,
        body.token_id[:20],
        body.price,
        trade_size,
        account.id,
    )

    result = _place_order_on_polymarket(
        private_key=stored["private_key"],
        clob_creds=stored.get("clob_creds"),
        token_id=body.token_id,
        side=body.side,
        price=body.price,
        size=trade_size,
    )

    # The recorder is the only sanctioned UserTrade constructor; it also
    # appends the order_* audit event on this same session.
    trade = persist_external_order(
        db,
        user_id=auth.user_id,
        market_id=body.market_id or body.token_id,
        token_id=body.token_id,
        action=body.side.lower(),
        amount=trade_size,
        price=body.price,
        status="pending" if result.get("success") else "failed",
        order_hash=result.get("order_hash"),
        account_id=account.id,
    )

    response = _order_response(
        result=result,
        trade_size=trade_size,
        price=body.price,
        side=body.side,
        trade_id=trade.id if trade is not None else None,
        account_id=account.id,
    )
    if idempotency_key:
        store_idempotent_order_result(db, auth.user_id, replay_key, response.model_dump())
    return response


# ── API-key management (internal, cookie/JWT) ───────────────


@api_keys_router.post(
    "",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_api_key_create_rate_limit)],
)
def create_api_key_route(
    request: ApiKeyCreateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Mint an API key. The secret is returned here and nowhere else.

    The digest is what persists; losing the secret means creating a new
    key. Scopes are validated against the allow-list — there is no
    ``withdraw`` scope to request.
    """
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    if request.account_id is not None:
        # A key scoped to somebody else's account is refused rather than
        # silently widened to the personal account.
        account = get_account_by_id(db, request.account_id)
        if account is None or account.owner_user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Account not found",
            )

    record, raw_secret = create_api_key_with_audit(
        db,
        user_id=user_id,
        name=request.name,
        account_id=request.account_id,
        scopes=request.scopes,
        expires_in_days=request.expires_in_days,
    )
    return ApiKeyCreatedResponse(**serialize_api_key(record, include_secret=raw_secret))


@api_keys_router.get("", response_model=list[ApiKeyResponse])
def list_api_keys_route(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List the caller's keys. Digests and secrets are never included."""
    records = list_api_keys(db, current_user.get("user_id"))
    return [ApiKeyResponse(**serialize_api_key(record)) for record in records]


@api_keys_router.delete("/{key_id}", response_model=ApiKeyResponse)
def revoke_api_key_route(
    key_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Revoke one of the caller's keys. Revocation is immediate and final."""
    record = revoke_api_key_with_audit(db, user_id=current_user.get("user_id"), key_id=key_id)
    if record is None:
        # 404 for both "missing" and "somebody else's": the endpoint is
        # not an existence oracle.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )
    return ApiKeyResponse(**serialize_api_key(record))


# ── Account exposure (internal, account-scoped) ────────────


def _exposure_rows(db: Session, user_id: int, account: Account) -> list[UserTrade]:
    """Open order rows for one account.

    Rows written before the accounts migration carry a NULL
    ``account_id``: they belong to the owner's personal account, which
    is what ``get_active_account`` resolves an absent header to, so they
    are included for the personal account only and never leak into a
    sub-account's exposure.
    """
    return list_exposure_trades(
        db,
        user_id=user_id,
        account_id=account.id if account is not None else None,
        personal_account=bool(account is not None and account.kind == AccountKind.PERSONAL.value),
        statuses=OPEN_TRADE_STATUSES,
        limit=MAX_EXPOSURE_ROWS,
    )


#: Hard cap on the rows an exposure read aggregates: the endpoint is a
#: compliance summary, not a trade browser.
MAX_EXPOSURE_ROWS = 5000


def build_exposure(db: Session, user_id: int, account: Account) -> ExposureResponse:
    """Aggregate open notional per market and per outcome.

    ``amount`` is treated as USDC notional for buys and shares for sells
    (the semantics the pre-trade gate already uses when it sums pending
    exposure), so gross/net notional and shares are both reported rather
    than collapsing them into one number a reviewer cannot audit.
    """
    buckets: dict[tuple[str, str | None], ExposureOutcome] = {}
    gross_notional = 0.0
    net_notional = 0.0
    for row in _exposure_rows(db, user_id, account):
        market_id = row.market_id or ""
        token_id = row.token_id or None
        bucket = buckets.get((market_id, token_id))
        if bucket is None:
            bucket = ExposureOutcome(market_id=market_id, token_id=token_id)
            buckets[(market_id, token_id)] = bucket
        amount = abs(_to_float(row.amount))
        price = _to_float(row.price)
        is_buy = (row.action or "").lower() != "sell"
        gross_notional += amount
        net_notional += amount if is_buy else -amount
        bucket.gross_notional += amount
        bucket.net_notional += amount if is_buy else -amount
        if price > 0:
            gross_shares = amount / price
            bucket.gross_shares += gross_shares
            bucket.net_shares += gross_shares if is_buy else -gross_shares
        bucket.orders += 1

    outcomes = sorted(
        buckets.values(),
        key=lambda item: (item.net_notional, item.market_id or "", item.token_id or ""),
    )
    limits: dict = {}
    if account is not None:
        settings = get_settings_row(db, account)
        limits = {
            "max_position_size": _to_float(getattr(settings, "max_position_size", 0.0)),
            "daily_loss_limit": _to_float(getattr(settings, "daily_loss_limit", 0.0)),
            "monthly_loss_limit": _to_float(getattr(settings, "monthly_loss_limit", 0.0)),
            "max_drawdown_pct": _to_float(getattr(settings, "max_drawdown_pct", 0.0)),
            "trading_halted": bool(getattr(settings, "trading_halted", False)),
        }
    return ExposureResponse(
        account_id=account.id if account is not None else 0,
        account_name=account.name if account is not None else "",
        accounts_enabled=get_settings().accounts_enabled,
        market_count=len({item.market_id for item in outcomes}),
        gross_notional=gross_notional,
        net_notional=net_notional,
        outcomes=outcomes,
        limits=limits,
    )


@exposure_router.get("/exposure", response_model=ExposureResponse)
def get_account_exposure(
    current_user: dict = Depends(get_current_user_from_token),
    active_account: Account = Depends(get_active_account),
    db: Session = Depends(get_db),
):
    """Per-market, per-outcome gross/net exposure for the active account.

    Scoped by the same ``X-Account-Id`` context every internal route
    uses, so the numbers are the active account's and nobody else's.
    """
    return build_exposure(db, current_user.get("user_id"), active_account)
