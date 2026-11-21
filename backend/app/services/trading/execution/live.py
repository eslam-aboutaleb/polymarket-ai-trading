"""Durable, opt-in live orders shared by manual and automated strategies.

A reservation and signed order hash commit before submission. An unknown ACK
never releases capital or causes another submission, including after restart.
Gross BUY principal is conservatively charged against risk budgets, even after
fills: unverified P&L must never replenish live spending authority.
"""

import hashlib
import json
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from sqlalchemy import and_, func, or_, select, update
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
from app.services.analytics.replay.ledger import accounting
from app.services.trading.execution import clob_transport
from app.services.trading.execution.order_state import (
    account_orders,
    audit,
    record_transition,
    snapshot,
    unresolved_orders,
    validate_observation,
)
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
) -> dict:
    owner_id = user_id if user_id is not None else settings.user_id
    if settings.simulation_mode or settings.trading_halted:
        raise LiveTradingBlocked("ACCOUNT_NOT_IN_LIVE_MODE")
    now = utc_now()
    cooldown = settings.cooldown_until
    if cooldown and cooldown.replace(tzinfo=now.tzinfo) > now:
        raise LiveTradingBlocked("COOLDOWN_ACTIVE")
    approval = {
        "approved_at": now.isoformat(),
        "requested_principal": str(notional),
        "owner_id": owner_id,
        "account_id": account_id,
        "budgets": [],
        "simulation_mode": False,
        "trading_halted": False,
    }
    if notional == 0:
        # Inventory-backed SELLs do not consume new purchase budgets.
        return approval
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
            query = query.where(account_orders(db, owner_id, account_id))
        if since:
            query = query.where(LiveOrder.created_at >= since)
        spent = Decimal(str(db.scalar(query)))
        if spent + notional > Decimal(str(limit)):
            raise LiveTradingBlocked(f"LIVE_{name.upper()}_BUDGET_EXCEEDED")
        approval["budgets"].append(
            {
                "name": name,
                "limit": str(limit),
                "reserved_before": str(spent),
                "window_start": since.isoformat() if since else None,
            }
        )
    return approval


def _authorize_copy(db, user_id, settings, copied_from_wallet, ai_approved):
    follow = db.scalar(
        select(FollowedTrader)
        .where(
            FollowedTrader.user_id == user_id,
            FollowedTrader.trader_wallet == (copied_from_wallet or "").lower(),
            FollowedTrader.is_active.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not settings.copy_trading_enabled or follow is None:
        raise LiveTradingBlocked("COPY_TRADING_NOT_ENABLED_FOR_TRADER")
    if settings.require_ai_approval and not ai_approved:
        raise LiveTradingBlocked("AI_APPROVAL_REQUIRED")


def _available_balance(db, client, user_id, token_id, side, shares, price_d, exclude_order_id=None):
    available = clob_transport.balance(client, token_id if side == "SELL" else None)
    balance_received_at = utc_now()
    required = shares if side == "SELL" else shares * price_d
    if side == "BUY":
        # Authenticated balance is a wallet balance, not proof that all
        # application commitments have already been deducted by the venue.
        committed = db.scalar(
            select(func.coalesce(func.sum(LiveOrder.reserved), 0)).where(
                LiveOrder.user_id == user_id,
                LiveOrder.id != exclude_order_id if exclude_order_id is not None else True,
                LiveOrder.side == "BUY",
                LiveOrder.state.in_(("SUBMITTING", "UNKNOWN", "SUBMITTED", "PARTIALLY_FILLED")),
            )
        )
        available -= Decimal(str(committed))
    if available < required:
        raise LiveTradingBlocked("INSUFFICIENT_AUTHENTICATED_BALANCE")
    if side == "SELL":
        pending = db.scalars(
            select(LiveOrder).where(
                LiveOrder.user_id == user_id,
                LiveOrder.id != exclude_order_id if exclude_order_id is not None else True,
                LiveOrder.token_id == token_id,
                LiveOrder.side == "SELL",
                LiveOrder.state.in_(("SUBMITTED", "PARTIALLY_FILLED")),
            )
        ).all()
        if sum((row.shares for row in pending), Decimal(0)) + shares > available:
            raise LiveTradingBlocked("SELL_INVENTORY_ALREADY_RESERVED")
    return available, balance_received_at


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
        "account_id": row.account_id,
        "reconciliation_required": bool(row.response.get("reconciliation_required")),
    }


@accounting
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
            _authorize_copy(db, user_id, settings, copied_from_wallet, ai_approved)
        if db.scalar(
            select(LiveOrder.id)
            .where(
                LiveOrder.user_id == user_id,
                unresolved_orders(),
            )
            .limit(1)
        ):
            raise LiveTradingBlocked("UNRESOLVED_LIVE_ORDER")
        wallet_approval = _risk_check(
            db, settings, shares * price_d if side == "BUY" else Decimal(0)
        )
        account_approval = None
        if account_settings is not None:
            account_approval = _risk_check(
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
        available, balance_received_at = _available_balance(
            db,
            client,
            user_id,
            token_id,
            side,
            shares,
            price_d,
        )
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
            or (side == "BUY" and notional > shares * price_d)
            or (side == "SELL" and notional < shares * price_d)
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
        from app.services.analytics.implementation_provenance import (
            configuration_fingerprint,
            implementation_fingerprint,
        )

        audit(
            db,
            row,
            "order_intent_created",
            {
                "trade_id": trade.id,
                "strategy": strategy,
                "side": side,
                "token_id": token_id,
                "condition_id": market_id,
                "fingerprint": fingerprint,
                "implementation_fingerprint": implementation_fingerprint(),
                "configuration_fingerprint": configuration_fingerprint(
                    db, user_id, account_id, strategy
                ),
                "price_limit": str(price_d),
                "shares": str(shares),
                "notional": str(notional),
                "order_type": order_type,
                "post_only": post_only,
                "state": "SUBMITTING",
                "venue": "polymarket-clob-v2",
                "chain_id": 137,
                "expiration_policy": "fill_or_kill" if order_type == "FOK" else "until_cancelled",
                "execution_deadline": execution_deadline.isoformat()
                if execution_deadline
                else None,
                "risk_approval": {
                    "policy_version": "conservative-principal-v1",
                    "wallet": wallet_approval,
                    "account": account_approval,
                    "authenticated_available": str(available),
                    "balance_received_at": balance_received_at.isoformat(),
                    "source_evidence_hash": hashlib.sha256(
                        json.dumps(execution_evidence, sort_keys=True).encode()
                    ).hexdigest()
                    if execution_evidence
                    else None,
                },
            },
        )
        db.commit()
        # Recovery/halt workers may have changed the committed intent while the
        # sender was between transactions. Never send using a cached projection.
        _lock_wallet(db, user_id, running=False)
        db.refresh(row, with_for_update=True)
        before = snapshot(row)
        if row.state != "SUBMITTING" or row.response.get("reconciliation_required"):
            db.commit()
            return order_result(row)
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
            if strategy == "copy":
                _authorize_copy(db, user_id, settings, copied_from_wallet, ai_approved)
            _available_balance(db, client, user_id, token_id, side, shares, price_d, row.id)
            require_enabled(strategy)
            check_quote_deadline()  # Balance I/O must not outlive the quote's authority.
        except LiveTradingBlocked as exc:
            # The exchange has not been called yet, so this rejection is certain.
            row.state, row.reserved, trade.status = "REJECTED", 0, "rejected"
            row.response = {"error": str(exc)}
            record_transition(db, row, before)
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
            rejected = response.get("success") is False or bool(response.get("errorMsg"))
            if rejected and not response.get("orderID"):
                row.state, row.reserved, trade.status = "REJECTED", 0, "rejected"
                row.response = {
                    "error": str(response.get("errorMsg") or "Exchange rejected order")[:500]
                }
            elif not rejected and response.get("orderID") == order_hash:
                row.state, trade.status = "SUBMITTED", "pending"
            else:
                row.state = "UNKNOWN"
                row.response = {
                    "error": "Unverified exchange acknowledgement; reconciliation required"
                }
        record_transition(db, row, before)
        db.commit()
        return order_result(row)
    except Exception:
        db.rollback()
        raise


@accounting
def reconcile_order(
    db: Session,
    *,
    user_id: int,
    order_hash: str,
    cancel: bool = False,
    account_id: int | None = None,
) -> dict:
    """Reconcile an owned signed order; absence never proves non-execution."""

    def owned_order():
        query = select(LiveOrder).where(
            LiveOrder.user_id == user_id,
            LiveOrder.order_hash == order_hash,
        )
        if account_id is not None:
            query = query.where(account_orders(db, user_id, account_id))
        row = db.scalar(query.with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise ValueError("Live order not found")
        return row

    try:
        user = _lock_wallet(db, user_id, running=False)
        row = owned_order()
        stored = load_wallet_credentials(user.wallet_address)
        if not stored:
            raise LiveTradingBlocked("TRADING_CREDENTIALS_REQUIRED")
        client = clob_transport.build_client(user.wallet_address, stored)
        if cancel and row.state not in {"FILLED", "CANCELLED", "REJECTED"}:
            audit(db, row, "order_cancel_requested", snapshot(row))
            db.commit()
            _lock_wallet(db, user_id, running=False)
            row = owned_order()
            if row.state not in {"FILLED", "CANCELLED", "REJECTED"}:
                try:
                    clob_transport.cancel(client, order_hash)
                except Exception:
                    audit(db, row, "order_cancel_unknown", snapshot(row))
        before = snapshot(row)
        trade = db.get(UserTrade, row.trade_id)
        try:
            observation = client.get_order(order_hash)
            matched, state = validate_observation(row, observation)
            if trade.filled_size is not None and matched < Decimal(str(trade.filled_size)):
                raise LiveTradingBlocked("EXCHANGE_ORDER_QUANTITY_CONFLICT")
            settled = (
                clob_transport.settled_fills(
                    client,
                    order_hash,
                    observation.get("associate_trades") or [],
                )
                if matched
                else None
            )
            if settled is not None:
                values = [settled[key] for key in ("shares", "notional", "price")]
                if (
                    any(not isinstance(v, Decimal) or not v.is_finite() for v in values)
                    or settled["shares"] != matched
                    or settled["notional"] <= 0
                    or not 0 < settled["price"] < 1
                    or (
                        row.side == "BUY"
                        and settled["notional"] > row.notional * matched / row.shares
                    )
                    or (
                        row.side == "SELL"
                        and settled["notional"] < row.notional * matched / row.shares
                    )
                ):
                    raise LiveTradingBlocked("EXCHANGE_FILL_ECONOMICS_CONFLICT")
                prior = row.response.get("filled_notional_exact")
                if prior is not None and (
                    settled["notional"] < Decimal(prior)
                    or (
                        matched == Decimal(str(row.response.get("confirmed_shares", 0)))
                        and settled["notional"] != Decimal(prior)
                    )
                ):
                    raise LiveTradingBlocked("EXCHANGE_FILL_ECONOMICS_CONFLICT")
                timestamp = settled.get("filled_at")
                created = row.created_at.replace(tzinfo=utc_now().tzinfo)
                if (
                    timestamp is None
                    or timestamp.tzinfo is None
                    or timestamp > utc_now()
                    or timestamp < created.replace(microsecond=0)
                ):
                    raise LiveTradingBlocked("EXCHANGE_FILL_TIMESTAMP_UNVERIFIED")
                fills = settled.get("fills")
                if not isinstance(fills, list) or not 0 < len(fills) <= 1000:
                    raise LiveTradingBlocked("EXCHANGE_FILL_IDENTITIES_UNVERIFIED")
                confirmed_ids = [fill["trade_id"] for fill in fills]
                if len(set(confirmed_ids)) != len(confirmed_ids) or not set(
                    row.response.get("confirmed_trade_ids", [])
                ).issubset(confirmed_ids):
                    raise LiveTradingBlocked("EXCHANGE_FILL_IDENTITIES_CONFLICT")
                receipts = {fill["trade_id"]: fill for fill in fills}
                if any(
                    receipts.get(prior["trade_id"]) != prior
                    for prior in row.response.get("confirmed_fills", [])
                ):
                    raise LiveTradingBlocked("EXCHANGE_FILL_IDENTITIES_CONFLICT")
            elif row.response.get("settled") or trade.status == "executed":
                raise LiveTradingBlocked("CONFIRMED_FILL_EVIDENCE_UNAVAILABLE")
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, LiveTradingBlocked)
                else "EXCHANGE_ORDER_STATE_UNVERIFIED"
            )
            row.response = {**row.response, "reconciliation_required": True, "error": reason}
            # A previously released remainder is uncertain again. Charge the
            # full signed principal until a consistent observation resolves it.
            if row.side == "BUY":
                row.reserved = max(row.reserved, row.notional)
            if not before["reconciliation_required"]:
                audit(db, row, "order_observation_rejected", {"reason": reason, "retained": before})
            db.commit()
            raise LiveTradingBlocked(reason) from exc

        row.state = state
        if state == "CANCELLED":
            row.reserved = (
                (row.notional * matched / row.shares).quantize(
                    Decimal("1e-12"), rounding=ROUND_CEILING
                )
                if row.side == "BUY"
                else 0
            )
            trade.status = "cancelled" if matched == 0 else "partially_filled"
        elif trade.status != "executed":
            trade.status = "pending"
        response = {
            **row.response,
            "status": str(observation["status"]).upper(),
            "size_matched": str(matched),
            "reconciliation_required": False,
            "error": None,
        }
        if settled is not None:
            previous_ids = set(row.response.get("confirmed_trade_ids", []))
            for fill in settled["fills"]:
                if fill["trade_id"] not in previous_ids:
                    audit(
                        db,
                        row,
                        "order_fill_confirmed",
                        {
                            **fill,
                            "matched_at": settled.get("fill_timestamps", {}).get(fill["trade_id"]),
                        },
                    )
            trade.filled_size = float(settled["shares"])
            trade.filled_price = float(settled["price"])
            trade.amount = float(settled["notional"])
            trade.price = float(settled["price"])
            trade.executed_at = settled["filled_at"]
            if matched == row.shares or state == "CANCELLED":
                trade.status = "executed"
            response.update(
                confirmed_trade_ids=sorted(confirmed_ids),
                confirmed_fills=sorted(settled["fills"], key=lambda fill: fill["trade_id"]),
                confirmed_shares=str(settled["shares"]),
                filled_notional_exact=str(settled["notional"]),
                filled_notional=float(settled["notional"]),
            )
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
        response["settled"] = trade.status == "executed"
        row.response = response
        record_transition(db, row, before)
        db.commit()
        return order_result(row)
    except Exception:
        db.rollback()
        raise


def reconcile_pending_live_orders(db: Session) -> None:
    """Scheduled recovery includes uncertain sends, partial fills and restarts."""
    import logging

    base = select(LiveOrder.id, LiveOrder.user_id, LiveOrder.order_hash).join(
        UserTrade,
        UserTrade.id == LiveOrder.trade_id,
    )
    rows = db.execute(
        base.where(
            or_(
                LiveOrder.response["reconciliation_required"].as_boolean().is_(True),
                and_(
                    LiveOrder.state.in_(
                        (
                            "SUBMITTING",
                            "UNKNOWN",
                            "SUBMITTED",
                            "PARTIALLY_FILLED",
                            "FILLED",
                            "CANCELLED",
                        )
                    ),
                    UserTrade.status.in_(("unknown", "pending", "partially_filled")),
                ),
            ),
        )
        .order_by(LiveOrder.updated_at, LiveOrder.id)
        .limit(100)
    ).all()
    # Keep a separate, fair terminal lane: late fills and settlement corrections
    # must be noticed after an earlier read marked a trade fully reconciled.
    # A terminal backlog cannot take capacity away from uncertain live orders.
    terminal = db.execute(
        base.where(
            LiveOrder.state.in_(("FILLED", "CANCELLED")),
            UserTrade.status.in_(("executed", "cancelled")),
            LiveOrder.response["reconciliation_required"].as_boolean().is_not(True),
        )
        .order_by(LiveOrder.updated_at, LiveOrder.id)
        .limit(20)
    ).all()
    rows = [*rows, *terminal]
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
