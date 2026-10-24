"""Stage-0 risk controls. Financial authority stays unavailable until reconciled."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.models.trading.trading_control import TradingControlState
from app.models.users.user import User
from app.services.trading.trading_halt_service import read_control, request_halt
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])


class HaltRequest(BaseModel):
    scope: Literal["user", "global"] = "user"


@router.get("/summary")
def risk_summary(
    current_user: dict = Depends(get_current_user_from_token), db: Session = Depends(get_db)
):
    from app.models.users.user_settings import UserSettings

    config = get_settings()
    global_control = read_control(db, "global")
    wallet_control = read_control(db, f"user:{current_user['user_id']}")
    settings = (
        db.query(UserSettings).filter(UserSettings.user_id == current_user["user_id"]).one_or_none()
    )
    ready = bool(
        config.live_trading_enabled
        and global_control["state"] == "RUNNING"
        and wallet_control["state"] == "RUNNING"
        and settings
        and not settings.simulation_mode
        and not settings.trading_halted
    )
    return {
        "global": global_control,
        "wallet": wallet_control,
        "live_trading_enabled": config.live_trading_enabled,
        "live_copy_trading_enabled": config.live_copy_trading_enabled,
        "live_execution_available": ready,
        "copy_trading_enabled": bool(settings and settings.copy_trading_enabled),
        "simulation_mode": bool(settings and settings.simulation_mode),
        "initial_capital": settings.initial_capital if settings else None,
        "financial_state": "CONSERVATIVE_PRINCIPAL_BUDGETS",
        "release_blocker": None if ready else "LIVE_FLAGS_AND_ACCOUNT_ACTIVATION_REQUIRED",
    }


@router.post("/halt", status_code=202)
def halt(
    body: HaltRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    user_id = current_user["user_id"]
    if body.scope == "global":
        actor = db.get(User, user_id, populate_existing=True)
        if actor is None or not actor.is_admin:
            raise HTTPException(status_code=403, detail="Global emergency role required")
    operations = request_halt(db, user_id=user_id, global_halt=body.scope == "global")
    # Global operators do not gain access to other users' operation reports.
    return {
        "halt_status": "HALTING",
        "scope": body.scope,
        "operations_queued": len(operations),
        "operation_ids": [op.id for op in operations if op.user_id == user_id],
    }


class RecoveryRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@router.post("/recovery-request", status_code=202)
def recovery_request(
    body: RecoveryRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    control = (
        db.query(TradingControlState)
        .filter(TradingControlState.scope == f"user:{current_user['user_id']}")
        .with_for_update()
        .one_or_none()
    )
    if control is None:
        raise HTTPException(status_code=409, detail="No verified halt state")
    control.state = "RECOVERY_REQUIRED"
    control.reason = f"Recovery requested: {body.reason}"
    control.version += 1
    control.updated_at = utc_now()
    db.commit()
    from sqlalchemy import func, select

    from app.models.trading.live_order import LiveOrder

    unresolved = db.scalar(
        select(func.count())
        .select_from(LiveOrder)
        .where(
            LiveOrder.user_id == current_user["user_id"],
            LiveOrder.state.in_(("UNKNOWN", "SUBMITTING")),
        )
    )
    return {
        "state": control.state,
        "resumed": False,
        "checks": {
            "unresolved_orders": unresolved,
            "activation_required": True,
            "activation_endpoint": "/api/v1/risk/activate",
        },
    }


class ActivateRequest(BaseModel):
    scope: Literal["user", "global"] = "user"
    initial_capital: float | None = Field(default=None, gt=0, allow_inf_nan=False)


@router.post("/activate")
def activate_live_trading(
    body: ActivateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Explicitly activate verified live execution; never called at startup."""
    from sqlalchemy import select

    from app.models.trading.live_order import LiveOrder
    from app.models.trading.trading_control import EmergencyHaltOperation
    from app.models.users.user_settings import UserSettings
    from app.security.credential_store import load_wallet_credentials
    from app.services.trading.execution import clob_transport
    from app.services.trading.execution.live import _lock_wallet, require_enabled
    from app.services.trading.live_trading_policy import LiveTradingBlocked

    user_id = current_user["user_id"]
    try:
        require_enabled("manual")
        actor = _lock_wallet(db, user_id, running=False)
        if body.scope == "global" and not actor.is_admin:
            raise HTTPException(403, "Global activation requires an administrator")
        unresolved = select(LiveOrder.id).where(LiveOrder.state.in_(("UNKNOWN", "SUBMITTING")))
        halts = select(EmergencyHaltOperation.id).where(EmergencyHaltOperation.state == "PENDING")
        if body.scope == "user":
            unresolved = unresolved.where(LiveOrder.user_id == user_id)
            halts = halts.where(EmergencyHaltOperation.user_id == user_id)
        if db.scalar(unresolved.limit(1)) or db.scalar(halts.limit(1)):
            raise LiveTradingBlocked("RECONCILIATION_REQUIRED")
        if body.scope == "user":
            global_state = db.get(TradingControlState, "global")
            if global_state is None or global_state.state != "RUNNING":
                raise LiveTradingBlocked("GLOBAL_LIVE_ACTIVATION_REQUIRED")
            stored = load_wallet_credentials(actor.wallet_address)
            if not stored:
                raise LiveTradingBlocked("TRADING_CREDENTIALS_REQUIRED")
            client = clob_transport.build_client(actor.wallet_address, stored)
            clob_transport.balance(client)
            open_orders = client.get_open_orders()
            if not isinstance(open_orders, list):
                raise LiveTradingBlocked("AUTHENTICATED_ORDERS_UNVERIFIED")
            owned = set(
                db.scalars(select(LiveOrder.order_hash).where(LiveOrder.user_id == user_id))
            )
            if any(order.get("id") not in owned for order in open_orders):
                raise LiveTradingBlocked("UNTRACKED_EXCHANGE_ORDERS_REQUIRE_CANCELLATION")
            settings = db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
            if settings is None or not settings.max_position_size or not settings.daily_loss_limit:
                raise LiveTradingBlocked("POSITION_AND_DAILY_BUDGETS_REQUIRED")
            if body.initial_capital is not None:
                settings.initial_capital = body.initial_capital
            if not settings.initial_capital:
                raise LiveTradingBlocked("INITIAL_CAPITAL_REQUIRED_FOR_LOSS_BUDGETS")
            settings.simulation_mode = False
            settings.trading_halted = False
            settings.halt_reason = None
            settings.cooldown_until = None
        scope = "global" if body.scope == "global" else f"user:{user_id}"
        control = db.get(TradingControlState, scope)
        if control is None:
            control = TradingControlState(
                scope=scope, user_id=user_id if body.scope == "user" else None, version=0
            )
            db.add(control)
        control.state = "RUNNING"
        control.reason = "Explicit live activation after gateway checks"
        control.version += 1
        control.updated_at = utc_now()
        db.commit()
        return {"scope": body.scope, "state": "RUNNING", "simulated": False}
    except LiveTradingBlocked as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.get("/orders")
def live_orders(
    current_user: dict = Depends(get_current_user_from_token), db: Session = Depends(get_db)
):
    from sqlalchemy import select

    from app.models.trading.live_order import LiveOrder
    from app.services.trading.execution.live import order_result

    rows = db.scalars(
        select(LiveOrder)
        .where(
            LiveOrder.user_id == current_user["user_id"],
        )
        .order_by(LiveOrder.id.desc())
        .limit(100)
    ).all()
    return [order_result(row) for row in rows]


@router.post("/orders/{order_hash}/reconcile")
def reconcile_live_order(
    order_hash: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    return _reconcile(db, current_user["user_id"], order_hash)


@router.post("/orders/{order_hash}/cancel")
def cancel_live_order(
    order_hash: str,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    return _reconcile(db, current_user["user_id"], order_hash, cancel=True)


def _reconcile(db: Session, user_id: int, order_hash: str, cancel: bool = False):
    from app.services.trading.execution.live import reconcile_order
    from app.services.trading.live_trading_policy import LiveTradingBlocked

    try:
        return reconcile_order(db, user_id=user_id, order_hash=order_hash, cancel=cancel)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LiveTradingBlocked as exc:
        raise HTTPException(409, str(exc)) from exc
