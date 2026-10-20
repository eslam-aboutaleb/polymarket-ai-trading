"""Persist halts before exchange work; never infer closure from submission.

Stage 0 deliberately has no resume or liquidation authority. A recovery
request records operator intent but cannot release the production barrier.
The wallet is currently user-scoped, so a halt covers every sub-account.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.trading.inverse_bot_position import InverseBotPosition
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.market_maker_config import MarketMakerConfig
from app.models.trading.trading_control import EmergencyHaltOperation, TradingControlState
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.utils.time import utc_now


def empty_report() -> dict:
    """Unknown counts remain null until an observation is available."""
    return {
        "open_orders_found": None,
        "cancel_requests_submitted": 0,
        "orders_confirmed_cancelled": 0,
        "orders_still_open": None,
        "orders_unresolved": None,
        "positions_found": None,
        "close_requests_submitted": 0,
        "positions_confirmed_closed": 0,
        "positions_partially_closed": 0,
        "unresolved_positions": None,
        "reconciliation_status": "PENDING",
        "errors": [],
    }


def read_control(db: Session, scope: str) -> dict:
    state = db.get(TradingControlState, scope, populate_existing=True)
    if state is None:
        return {"scope": scope, "state": "RECOVERY_REQUIRED", "reason": "STATE_UNVERIFIED"}
    return {
        "scope": state.scope,
        "state": state.state,
        "reason": state.reason,
        "version": state.version,
        "updated_at": state.updated_at.isoformat(),
    }


def _halt_wallet(
    db: Session, user_id: int, close_positions: bool, *, requested_by_user_id: int
) -> EmergencyHaltOperation:
    # A real row lock coordinates requests across processes; no Redis authority.
    user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one()
    scope = f"user:{user.id}"
    control = db.scalars(
        select(TradingControlState)
        .where(TradingControlState.scope == scope)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if control is None:
        control = TradingControlState(scope=scope, user_id=user.id, version=0)
        db.add(control)
    control.state = "HALTING"
    control.reason = "Emergency halt requested; exchange cancellation pending"
    control.version += 1
    control.updated_at = utc_now()

    settings = db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    ).scalar_one_or_none()
    if settings is None:
        settings = UserSettings(user_id=user.id)
        db.add(settings)
    settings.trading_halted = True
    settings.copy_trading_enabled = False
    settings.inverse_bot_enabled = False
    settings.halt_reason = control.reason
    # Preserve conditional triggers and positions for the later exit migration.
    # Disabling a local strategy does not assert that its exchange orders stopped.
    db.query(MarketMakerConfig).filter(MarketMakerConfig.user_id == user.id).update(
        {"enabled": False, "status": "paused"}, synchronize_session=False
    )
    db.query(LatencyArbConfig).filter(LatencyArbConfig.user_id == user.id).update(
        {"enabled": False}, synchronize_session=False
    )
    db.query(InverseBotPosition).filter(InverseBotPosition.user_id == user.id).update(
        {"enabled": False}, synchronize_session=False
    )
    operation = EmergencyHaltOperation(
        user_id=user.id,
        requested_by_user_id=requested_by_user_id,
        close_positions=close_positions,
        control_version=control.version,
        report=empty_report(),
    )
    db.add(operation)
    db.flush()
    return operation


def request_halt(
    db: Session, *, user_id: int, close_positions: bool = False, global_halt: bool = False
) -> list[EmergencyHaltOperation]:
    """Commit halt and cancellation outbox atomically, before any network call."""
    try:
        if global_halt:
            control = db.execute(
                select(TradingControlState)
                .where(TradingControlState.scope == "global")
                .with_for_update()
            ).scalar_one()  # Missing migration/state fails closed.
            control.state = "HALTING"
            control.reason = "Global emergency halt requested"
            control.version += 1
            control.updated_at = utc_now()
            users = db.scalars(select(User.id).order_by(User.id)).all()
        else:
            users = [user_id]
        operations = [
            _halt_wallet(db, uid, close_positions, requested_by_user_id=user_id) for uid in users
        ]
        db.commit()
        return operations
    except Exception:
        db.rollback()
        raise


def operation_status(db: Session, *, operation_id: str, user_id: int) -> dict | None:
    operation = db.execute(
        select(EmergencyHaltOperation).where(
            EmergencyHaltOperation.id == operation_id,
            EmergencyHaltOperation.user_id == user_id,
        )
    ).scalar_one_or_none()
    if operation is None:
        return None
    return {
        "operation_id": operation.id,
        "halted": True,
        "halt_status": read_control(db, f"user:{user_id}")["state"],
        "operation_status": operation.state,
        "close_positions_requested": operation.close_positions,
        **operation.report,
        "message": (
            "Trading halt persisted. Cancellation verification is pending; "
            "positions may remain open."
            if operation.state == "PENDING"
            else "Trading remains halted. Review cancellation results and unresolved positions."
        ),
    }
