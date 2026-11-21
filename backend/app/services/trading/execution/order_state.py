"""Signed-order observation checks and transactional, append-only gateway audit."""

import json
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.trading.live_order import LiveOrder
from app.models.users.account import Account, AccountKind
from app.services.trading.live_trading_policy import LiveTradingBlocked


def unresolved_orders():
    """Uncertain sends and conflicting observations both block additional risk."""
    return or_(
        LiveOrder.state.in_(("SUBMITTING", "UNKNOWN")),
        LiveOrder.response["reconciliation_required"].as_boolean().is_(True),
    )


def account_orders(db: Session, user_id: int, account_id: int):
    """Legacy wallet-only intents belong to personal, never to a subaccount."""
    account = db.get(Account, account_id)
    if account is None or account.owner_user_id != user_id:
        raise ValueError("Live order not found")
    scoped = LiveOrder.account_id == account_id
    if account.kind == AccountKind.PERSONAL.value:
        return or_(scoped, LiveOrder.account_id.is_(None))
    return scoped


def audit(db: Session, row: LiveOrder, event_type: str, payload: dict) -> None:
    from app.services.platform.event_log import MAX_PAYLOAD_CHARS, append

    # A truncated intent is not durable approval evidence. Fail before the
    # exchange mutation instead of accepting the general logger's truncation.
    if len(json.dumps(payload, default=str)) > MAX_PAYLOAD_CHARS:
        raise LiveTradingBlocked("ORDER_AUDIT_PAYLOAD_TOO_LARGE")
    if (
        append(
            db,
            event_type=event_type,
            aggregate_type="order",
            aggregate_id=row.order_hash,
            user_id=row.user_id,
            account_id=row.account_id,
            payload=payload,
            source="live_gateway",
        )
        is None
    ):
        raise LiveTradingBlocked("DURABLE_ORDER_AUDIT_REQUIRED")


def snapshot(row: LiveOrder) -> dict:
    notional = row.response.get("filled_notional_exact")
    return {
        "state": row.state,
        "reserved": str(Decimal(str(row.reserved)).normalize()),
        "size_matched": str(Decimal(str(row.response.get("size_matched", 0))).normalize()),
        "settled": bool(row.response.get("settled")),
        "filled_notional_exact": (
            str(Decimal(notional).normalize()) if notional is not None else None
        ),
        "reconciliation_required": bool(row.response.get("reconciliation_required")),
    }


def record_transition(db: Session, row: LiveOrder, before: dict) -> None:
    after = snapshot(row)
    if before != after:
        audit(db, row, "order_state_changed", {"before": before, "after": after})


def validate_observation(row: LiveOrder, observation: dict) -> tuple[Decimal, str]:
    """Never let stale or mismatched evidence reduce a durable exposure."""
    if not isinstance(observation, dict) or observation.get("id") != row.order_hash:
        raise LiveTradingBlocked("EXCHANGE_ORDER_STATE_UNVERIFIED")
    try:
        matched = Decimal(str(observation["size_matched"]))
        original = Decimal(str(observation["original_size"]))
        previous = Decimal(str(row.response.get("size_matched", 0)))
        if (
            not matched.is_finite()
            or not original.is_finite()
            or not previous.is_finite()
            or original != row.shares
            or matched < 0
            or matched > original
            or matched < previous
        ):
            raise ValueError("Quantity conflicts with signed order or previous observation")
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise LiveTradingBlocked("EXCHANGE_ORDER_QUANTITY_CONFLICT") from exc
    if (
        observation.get("asset_id", row.token_id) != row.token_id
        or observation.get("side", row.side) != row.side
    ):
        raise LiveTradingBlocked("EXCHANGE_ORDER_IDENTITY_CONFLICT")
    status = str(observation.get("status", "")).upper()
    if status in ("CANCELED", "CANCELLED"):
        state = "FILLED" if matched == original else "CANCELLED"
    elif status == "MATCHED" and matched == original:
        state = "FILLED"
    elif status in ("LIVE", "MATCHED", "DELAYED"):
        state = "PARTIALLY_FILLED" if matched else "SUBMITTED"
    else:
        raise LiveTradingBlocked("EXCHANGE_ORDER_STATE_UNVERIFIED")
    if (
        (row.state == "FILLED" and state != "FILLED")
        or (row.state == "CANCELLED" and state not in ("CANCELLED", "FILLED"))
        or row.state == "REJECTED"
    ):
        raise LiveTradingBlocked("EXCHANGE_ORDER_STATE_REGRESSION")
    return matched, state
