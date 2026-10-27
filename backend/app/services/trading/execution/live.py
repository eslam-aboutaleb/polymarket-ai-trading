"""Durable, opt-in live orders shared by manual and automated strategies.

A reservation and signed order hash commit before submission. An unknown ACK
never releases capital or causes another submission, including after restart.
Gross BUY principal is conservatively charged against risk budgets, even after
fills: unverified P&L must never replenish live spending authority.
"""

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.live_order import LiveOrder
from app.models.trading.trading_control import TradingControlState
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account, AccountKind
from app.models.users.account_settings import AccountSettings
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.security.credential_store import load_wallet_credentials
from app.services.trading.execution import clob_transport
from app.services.trading.live_trading_policy import LiveTradingBlocked
from app.utils.time import utc_now

STRATEGY_FLAGS = {
    "manual": None,
    "external": None,
    "copy": "live_copy_trading_enabled",
    "stop_loss": None,
    "take_profit": None,
    "inverse": "live_inverse_trading_enabled",
    "market_maker": "live_market_making_enabled",
    "latency_arb": "live_latency_arb_enabled",
}


def require_enabled(strategy: str) -> None:
    config = get_settings()
    if not config.live_trading_enabled:
        raise LiveTradingBlocked("LIVE_TRADING_DISABLED")
    if strategy not in STRATEGY_FLAGS:
        raise LiveTradingBlocked("LIVE_STRATEGY_UNSUPPORTED")
    flag = STRATEGY_FLAGS[strategy]
    if flag and not getattr(config, flag):
        raise LiveTradingBlocked("LIVE_STRATEGY_DISABLED")


def _lock_wallet(db: Session, user_id: int, *, running: bool = True) -> User:
    # Lock order matches global/user emergency halts. Hold locks during the
    # exchange call so a halt cannot acknowledge before an in-flight send ends.
    global_control = db.scalar(
        select(TradingControlState)
        .where(TradingControlState.scope == "global")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    control = db.scalar(
        select(TradingControlState)
        .where(TradingControlState.scope == f"user:{user_id}")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise LiveTradingBlocked("USER_NOT_FOUND")
    if running and any(c is None or c.state != "RUNNING" for c in (global_control, control)):
        raise LiveTradingBlocked("TRADING_HALTED_OR_RECOVERY_REQUIRED")
    return user


def _decimal(value, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"Invalid {name}")
    return number


def _risk_check(
    db: Session,
    settings: UserSettings | AccountSettings,
    notional: Decimal,
    *,
    user_id: int | None = None,
    account_id: int | None = None,
    exclude_order_id: int | None = None,
) -> None:
    owner_id = user_id if user_id is not None else settings.user_id
    if settings.simulation_mode or settings.trading_halted:
        raise LiveTradingBlocked("ACCOUNT_NOT_IN_LIVE_MODE")
    now = utc_now()
    cooldown = settings.cooldown_until
    if cooldown and cooldown.replace(tzinfo=now.tzinfo) > now:
        raise LiveTradingBlocked("COOLDOWN_ACTIVE")
    if notional == 0:
        # Inventory-backed SELLs do not consume new purchase budgets.
        return
    if (
        (settings.max_drawdown_pct or 0) > 0 or (settings.total_loss_halt_pct or 0) > 0
    ) and not settings.initial_capital:
        raise LiveTradingBlocked("INITIAL_CAPITAL_REQUIRED_FOR_LOSS_BUDGETS")
    # Legacy fills do not have the durable reservation accounting required here.
    legacy = db.scalar(
        select(UserTrade.id)
        .where(
            UserTrade.user_id == owner_id,
            UserTrade.status.in_(("pending", "executed", "filled", "unknown")),
            ~UserTrade.id.in_(select(LiveOrder.trade_id)),
        )
        .limit(1)
    )
    if legacy is not None:
        raise LiveTradingBlocked("LEGACY_EXPOSURE_REQUIRES_RECONCILIATION")
    for name, limit, since in (
        ("position", settings.max_position_size, None),
        (
            "daily",
            settings.daily_loss_limit,
            now.replace(hour=0, minute=0, second=0, microsecond=0),
        ),
        (
            "monthly",
            settings.monthly_loss_limit,
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        ),
        (
            "drawdown",
            (settings.initial_capital or 0) * (settings.max_drawdown_pct or 0) / 100,
            None,
        ),
        (
            "total_loss",
            (settings.initial_capital or 0) * (settings.total_loss_halt_pct or 0) / 100,
            None,
        ),
    ):
        if name in ("position", "daily") and (limit is None or not float(limit) > 0):
            raise LiveTradingBlocked(f"LIVE_{name.upper()}_BUDGET_REQUIRED")
        if limit is None or not float(limit) > 0:
            continue
        if not Decimal(str(limit)).is_finite():
            raise LiveTradingBlocked(f"LIVE_{name.upper()}_BUDGET_INVALID")
        query = select(func.coalesce(func.sum(LiveOrder.reserved), 0)).where(
            LiveOrder.user_id == owner_id,
        )
        if exclude_order_id is not None:
            query = query.where(LiveOrder.id != exclude_order_id)
        if account_id is not None:
            query = query.where(LiveOrder.account_id == account_id)
        if since:
            query = query.where(LiveOrder.created_at >= since)
        spent = Decimal(str(db.scalar(query)))
        if spent + notional > Decimal(str(limit)):
            raise LiveTradingBlocked(f"LIVE_{name.upper()}_BUDGET_EXCEEDED")


def order_result(row: LiveOrder) -> dict:
    return {
        "success": row.state in ("SUBMITTED", "FILLED", "PARTIALLY_FILLED"),
        "status": row.state.lower(),
        "order_hash": row.order_hash,
        "trade_id": row.trade_id,
        "trade_size": float(row.notional),
        "shares": float(row.shares),
        "simulated": False,
        "error": row.response.get("error"),
        "settled": bool(row.response.get("settled")),
        "filled_notional": row.response.get("filled_notional"),
    }


def place_live_order(
    db: Session,
    *,
    user_id: int,
    token_id: str,
    side: str,
    price: float,
    size: float,
    idempotency_key: str,
    strategy: str = "manual",
    market_id: str | None = None,
    copied_from_wallet: str | None = None,
    source_trade_history_id: int | None = None,
    ai_approved: bool = False,
    order_type: str = "GTC",
    post_only: bool = False,
    account_id: int | None = None,
    execution_deadline: datetime | None = None,
    execution_evidence: dict | None = None,
) -> dict:
    """size is shares for both sides; all persisted money amounts are notional."""

    def check_quote_deadline():
        if strategy == "latency_arb" and (
            execution_deadline is None
            or execution_deadline.tzinfo is None
            or utc_now() >= execution_deadline
        ):
            raise LiveTradingBlocked("EXECUTABLE_QUOTE_EXPIRED_OR_MISSING")

    require_enabled(strategy)
    check_quote_deadline()
    price_d, shares = _decimal(price, "price"), _decimal(size, "size")
    if price_d >= 1 or side not in ("BUY", "SELL") or not token_id:
        raise ValueError("Invalid live order")
    if shares != shares.quantize(Decimal("0.01")):
        raise ValueError("Live share size must have at most two decimal places")
    if order_type not in ("GTC", "FOK") or (post_only and order_type != "GTC"):
        raise ValueError("Unsupported live order type")
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValueError("A stable Idempotency-Key (at most 200 characters) is required")
    payload = [
        token_id,
        side,
        str(price_d.normalize()),
        str(shares.normalize()),
        strategy,
        market_id,
        copied_from_wallet,
        source_trade_history_id,
        order_type,
        post_only,
        account_id,
    ]
    fingerprint = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    try:
        user = _lock_wallet(db, user_id)
        existing = db.scalar(
            select(LiveOrder).where(
                LiveOrder.user_id == user_id,
                LiveOrder.idempotency_key == idempotency_key,
            )
        )
        if existing:
            if existing.fingerprint != fingerprint:
                raise ValueError("Idempotency-Key already belongs to a different order")
            result = order_result(existing)
            db.commit()
            return result
        settings = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        if settings is None:
            raise LiveTradingBlocked("TRADING_SETTINGS_REQUIRED")
        account_settings = None
        if account_id is not None:
            account = db.get(Account, account_id, populate_existing=True)
            if account is None or account.owner_user_id != user_id:
                raise LiveTradingBlocked("ACCOUNT_OWNERSHIP_REQUIRED")
            if account.kind != AccountKind.PERSONAL.value:
                account_settings = db.scalar(
                    select(AccountSettings)
                    .where(
                        AccountSettings.account_id == account_id,
                    )
                    .execution_options(populate_existing=True)
                )
                if account_settings is None:
                    raise LiveTradingBlocked("ACCOUNT_SETTINGS_REQUIRED")
        if strategy == "copy":
            follow = db.scalar(
                select(FollowedTrader).where(
                    FollowedTrader.user_id == user_id,
                    FollowedTrader.trader_wallet == (copied_from_wallet or "").lower(),
                    FollowedTrader.is_active.is_(True),
                )
            )
            if not settings.copy_trading_enabled or follow is None:
                raise LiveTradingBlocked("COPY_TRADING_NOT_ENABLED_FOR_TRADER")
            if settings.require_ai_approval and not ai_approved:
                raise LiveTradingBlocked("AI_APPROVAL_REQUIRED")
        if db.scalar(
            select(LiveOrder.id)
            .where(
                LiveOrder.user_id == user_id,
                LiveOrder.state.in_(("SUBMITTING", "UNKNOWN")),
            )
            .limit(1)
        ):
            raise LiveTradingBlocked("UNRESOLVED_LIVE_ORDER")
        _risk_check(db, settings, shares * price_d if side == "BUY" else Decimal(0))
        if account_settings is not None:
            _risk_check(
                db,
                account_settings,
                shares * price_d if side == "BUY" else Decimal(0),
                user_id=user_id,
                account_id=account_id,
            )
        stored = load_wallet_credentials(user.wallet_address)
        if not stored:
            raise LiveTradingBlocked("TRADING_CREDENTIALS_REQUIRED")
        client = clob_transport.build_client(user.wallet_address, stored)
        available = clob_transport.balance(client, token_id if side == "SELL" else None)
        required = shares if side == "SELL" else shares * price_d
        if available < required:
            raise LiveTradingBlocked("INSUFFICIENT_AUTHENTICATED_BALANCE")
        if side == "SELL":
            pending = db.scalars(
                select(LiveOrder).where(
                    LiveOrder.user_id == user_id,
                    LiveOrder.token_id == token_id,
                    LiveOrder.side == "SELL",
                    LiveOrder.state.in_(("SUBMITTED", "PARTIALLY_FILLED")),
                )
            ).all()
            if sum((row.shares for row in pending), Decimal(0)) + shares > available:
                raise LiveTradingBlocked("SELL_INVENTORY_ALREADY_RESERVED")
        check_quote_deadline()
        authorized_shares = shares
        quote_options = {}
        if strategy == "latency_arb" and execution_evidence:
            quote_options = {
                "expected_tick": Decimal(execution_evidence["tick_size"]),
                "expected_neg_risk": execution_evidence["neg_risk"],
                "execution_deadline": execution_deadline,
            }
        signed, order_hash, shares, notional = clob_transport.prepare(
            client, token_id, side, price_d, shares, **quote_options
        )
        if (
            shares <= 0
            or shares > authorized_shares
            or notional <= 0
            or notional > required
            and side == "BUY"
        ):
            raise ValueError("Signed order amounts exceed the authorized order")
        trade = UserTrade(
            user_id=user_id,
            account_id=account_id,
            market_id=market_id or token_id,
            token_id=token_id,
            action=side.lower(),
            amount=float(notional),
            price=float(price_d),
            status="unknown",
            order_hash=order_hash,
            strategy_source=strategy,
            copied_from_wallet=copied_from_wallet,
            source_trade_history_id=source_trade_history_id,
            expected_price=float(price_d),
            expected_size=float(notional),
            calculation_details=json.dumps({"executable_book": execution_evidence})
            if execution_evidence
            else None,
        )
        db.add(trade)
        db.flush()  # Recording is mandatory before the money path.
        row = LiveOrder(
            user_id=user_id,
            account_id=account_id,
            trade_id=trade.id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            strategy=strategy,
            token_id=token_id,
            side=side,
            price=price_d,
            shares=shares,
            notional=notional,
            reserved=notional if side == "BUY" else 0,
            state="SUBMITTING",
            order_hash=order_hash,
            response={},
        )
        db.add(row)
        from app.services.platform.event_log import append

        audit = append(
            db,
            event_type="order_intent_created",
            aggregate_type="order",
            aggregate_id=order_hash,
            user_id=user_id,
            account_id=account_id,
            payload={
                "trade_id": trade.id,
                "strategy": strategy,
                "side": side,
                "notional": str(notional),
                "state": "SUBMITTING",
            },
            source="live_gateway",
        )
        if audit is None:
            raise LiveTradingBlocked("DURABLE_ORDER_AUDIT_REQUIRED")
        db.commit()
        try:
            _lock_wallet(db, user_id)  # Recheck halt/configuration after preparation.
            require_enabled(strategy)
            db.refresh(settings, with_for_update=True)
            debit = notional if side == "BUY" else Decimal(0)
            _risk_check(db, settings, debit, exclude_order_id=row.id)
            if account_settings is not None:
                db.refresh(account_settings, with_for_update=True)
                _risk_check(
                    db,
                    account_settings,
                    debit,
                    user_id=user_id,
                    account_id=account_id,
                    exclude_order_id=row.id,
                )
            check_quote_deadline()
            if strategy == "copy" and not settings.copy_trading_enabled:
                raise LiveTradingBlocked("COPY_TRADING_DISABLED")
        except LiveTradingBlocked as exc:
            # The exchange has not been called yet, so this rejection is certain.
            row.state, row.reserved, trade.status = "REJECTED", 0, "rejected"
            row.response = {"error": str(exc)}
            db.commit()
            return order_result(row)
        try:
            response = clob_transport.submit(client, signed, order_type, post_only)
        except Exception:
            # A timeout or malformed ACK is not a rejection. Retain the hash and
            # reservation and require authenticated reconciliation; never retry.
            row.state = "UNKNOWN"
            row.response = {"error": "Exchange outcome unknown; reconcile before submitting again"}
        else:
            if not isinstance(response, dict):
                response = {}
            row.response = {
                k: response[k]
                for k in ("status", "orderID", "errorMsg", "success")
                if k in response
            }
            if response.get("success") is False or response.get("errorMsg"):
                row.state, row.reserved, trade.status = "REJECTED", 0, "rejected"
                row.response = {
                    "error": str(response.get("errorMsg") or "Exchange rejected order")[:500]
                }
            elif response.get("orderID") == order_hash:
                row.state, trade.status = "SUBMITTED", "pending"
            else:
                row.state = "UNKNOWN"
                row.response = {
                    "error": "Unverified exchange acknowledgement; reconciliation required"
                }
        db.commit()
        return order_result(row)
    except Exception:
        db.rollback()
        raise


def reconcile_order(db: Session, *, user_id: int, order_hash: str, cancel: bool = False) -> dict:
    """Query one owned order. Absence/404 never proves it did not execute."""
    try:
        user = _lock_wallet(db, user_id, running=False)
        row = db.scalar(
            select(LiveOrder)
            .where(
                LiveOrder.user_id == user_id,
                LiveOrder.order_hash == order_hash,
            )
            .with_for_update()
        )
        if row is None:
            raise ValueError("Live order not found")
        stored = load_wallet_credentials(user.wallet_address)
        if not stored:
            raise LiveTradingBlocked("TRADING_CREDENTIALS_REQUIRED")
        client = clob_transport.build_client(user.wallet_address, stored)
        if cancel:
            clob_transport.cancel(client, order_hash)
        observation = client.get_order(order_hash)
        if not isinstance(observation, dict) or observation.get("id") != order_hash:
            raise LiveTradingBlocked("EXCHANGE_ORDER_STATE_UNVERIFIED")
        matched = Decimal(str(observation["size_matched"]))
        original = Decimal(str(observation["original_size"]))
        if (
            not matched.is_finite()
            or not original.is_finite()
            or matched < 0
            or original <= 0
            or matched > original
        ):
            raise ValueError("Invalid exchange order quantities")
        status = str(observation["status"]).upper()
        trade = db.get(UserTrade, row.trade_id)
        if status in ("CANCELED", "CANCELLED"):
            row.state = "CANCELLED"
            # Charge filled principal; only the cancelled remainder is released.
            row.reserved = row.notional * matched / original if row.side == "BUY" else 0
            trade.status = "cancelled" if matched == 0 else "partially_filled"
        elif matched == original and status == "MATCHED":
            row.state = "FILLED"
            # Matching does not prove settlement or a fill price. The trade
            # reconciler must observe fills before recognizing P&L.
            trade.status = "pending"
        elif status in ("LIVE", "MATCHED", "DELAYED"):
            row.state = "PARTIALLY_FILLED" if matched else "SUBMITTED"
            trade.status = "pending"
        else:
            raise LiveTradingBlocked("EXCHANGE_ORDER_STATE_UNVERIFIED")
        settled = (
            clob_transport.settled_fills(
                client,
                order_hash,
                observation.get("associate_trades") or [],
            )
            if matched
            else None
        )
        if settled and settled["shares"] == matched:
            trade.filled_size = float(settled["shares"])
            trade.filled_price = float(settled["price"])
            trade.amount = float(settled["notional"])
            trade.price = float(settled["price"])
            trade.executed_at = settled.get("filled_at") or trade.executed_at
            if matched == original or row.state == "CANCELLED":
                trade.status = "executed"
            from app.models.trading.stop_loss import StopLossOrder
            from app.models.trading.take_profit import TakeProfitOrder

            for model in (StopLossOrder, TakeProfitOrder):
                conditional = db.scalar(
                    select(model).where(
                        model.user_id == user_id,
                        model.order_hash == order_hash,
                    )
                )
                if conditional is not None:
                    conditional.executed_price = float(settled["price"])
            # No invented fee or P&L. Principal remains reserved after fills.
        row.response = {
            "status": status,
            "size_matched": str(matched),
            "settled": trade.status == "executed",
            "filled_notional": float(settled["notional"]) if settled else None,
        }
        db.commit()
        return order_result(row)
    except Exception:
        db.rollback()
        raise


def reconcile_pending_live_orders(db: Session) -> None:
    """Scheduled recovery includes uncertain sends, partial fills and restarts."""
    import logging

    rows = db.execute(
        select(LiveOrder.id, LiveOrder.user_id, LiveOrder.order_hash)
        .join(
            UserTrade,
            UserTrade.id == LiveOrder.trade_id,
        )
        .where(
            LiveOrder.state.in_(
                ("SUBMITTING", "UNKNOWN", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED")
            ),
            UserTrade.status.in_(("unknown", "pending", "partially_filled")),
        )
        .order_by(LiveOrder.updated_at, LiveOrder.id)
        .limit(100)
    ).all()
    for row_id, user_id, order_hash in rows:
        try:
            reconcile_order(db, user_id=user_id, order_hash=order_hash)
        except Exception:
            db.rollback()
            logging.getLogger(__name__).warning(
                "Live order reconciliation remains unresolved: %s", order_hash
            )
        finally:
            # Failed observations must also move behind unattempted orders.
            # Otherwise a full batch of permanent 404s or expired credentials
            # can prevent every later order from ever being reconciled.
            db.execute(update(LiveOrder).where(LiveOrder.id == row_id).values(updated_at=utc_now()))
            db.commit()
