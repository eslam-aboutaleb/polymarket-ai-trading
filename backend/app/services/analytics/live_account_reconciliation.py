"""Account-scoped native balance observations and confirmed-fill read side (#3).

No signing, submission, balance credits or UserTrade/P&L writes. Known-token
coverage and missing fees/finality remain explicit, never invented earnings.
"""

import hashlib
import json
import time
from datetime import UTC, datetime

from sqlalchemy import func, or_, select

from app.models.analytics.live_balance_snapshot import LiveBalanceSnapshot
from app.models.platform.app_event import AppEvent
from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.security.credential_store import load_wallet_credentials
from app.services.analytics.pnl_reconciliation import reconcile_confirmed_cashflows
from app.services.trading.execution import clob_transport
from app.services.trading.execution.order_state import account_orders
from app.utils.time import utc_now

VERSION = "live-attribution-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=lambda obj: str(obj))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def account_events(account):
    clause = AppEvent.account_id == account.id
    return or_(clause, AppEvent.account_id.is_(None)) if account.kind == "personal" else clause


def capture_balance_snapshot(db, account):
    """Observe fixed native endpoints with existing credentials; no mutation API."""
    try:
        user = db.get(User, account.owner_user_id)
        if user is None:
            raise ValueError("ACCOUNT_OWNER_UNAVAILABLE")
        watermark = db.scalar(select(func.max(AppEvent.id)).where(AppEvent.user_id == user.id)) or 0
        if account.kind != "personal":
            raise ValueError("SUBACCOUNT_WALLET_ALLOCATION_UNVERIFIED")
        orders = db.scalars(select(LiveOrder).where(LiveOrder.user_id == user.id).limit(501)).all()
        if len(orders) > 500:
            raise ValueError("ORDER_HISTORY_BOUND_EXCEEDED")
        if any(order.account_id not in (None, account.id) for order in orders):
            raise ValueError("SHARED_WALLET_ACCOUNT_ALLOCATION_UNVERIFIED")
        tokens = sorted({order.token_id for order in orders})
        if len(tokens) > 20 or any(not token.isdecimal() or len(token) > 78 for token in tokens):
            raise ValueError("KNOWN_TOKEN_OBSERVATION_BOUND_EXCEEDED")
        stored = load_wallet_credentials(user.wallet_address)
        if not stored or not stored.get("clob_creds"):
            raise ValueError("AUTHENTICATED_READ_CREDENTIALS_REQUIRED")
        client = clob_transport.build_client(user.wallet_address, stored)
        started_at, deadline = utc_now(), time.monotonic() + 10
        cash = clob_transport.balance(client)
        positions = {}
        for token in tokens:
            if time.monotonic() >= deadline:
                raise ValueError("BALANCE_OBSERVATION_DEADLINE_EXCEEDED")
            positions[token] = str(clob_transport.balance(client, token))
        if time.monotonic() >= deadline:
            raise ValueError("BALANCE_OBSERVATION_DEADLINE_EXCEEDED")
        if clob_transport.balance(client) != cash:
            raise ValueError("BALANCE_CHANGED_DURING_OBSERVATION")
        if time.monotonic() >= deadline:
            raise ValueError("BALANCE_OBSERVATION_DEADLINE_EXCEEDED")
        active = any(
            order.state not in {"FILLED", "CANCELLED", "REJECTED"}
            or order.response.get("reconciliation_required")
            for order in orders
        )
        unsettled = (
            db.scalar(
                select(UserTrade.id)
                .join(LiveOrder, LiveOrder.trade_id == UserTrade.id)
                .where(
                    LiveOrder.user_id == user.id,
                    UserTrade.status.in_(("unknown", "pending", "partially_filled")),
                )
                .limit(1)
            )
            is not None
        )
        if (
            db.scalar(select(func.max(AppEvent.id)).where(AppEvent.user_id == user.id)) or 0
        ) != watermark:
            raise ValueError("ACTIVITY_CHANGED_DURING_OBSERVATION")
        now = utc_now()
        evidence = {
            "schema_version": VERSION,
            "user_id": user.id,
            "account_id": account.id,
            "started_at": started_at.isoformat(),
            "observed_at": now.isoformat(),
            "wallet_identity": hashlib.sha256(
                (
                    user.wallet_address.lower()
                    + ":"
                    + str(stored.get("funder") or user.wallet_address).lower()
                ).encode()
            ).hexdigest(),
            "provider": "authenticated_clob_v2",
            "coverage": "known_application_tokens",
            "cash_usdc": str(cash),
            "positions": positions,
            "active_or_unsettled_orders": active or unsettled,
            "event_watermark": watermark,
        }
        row = LiveBalanceSnapshot(
            id=digest(evidence),
            user_id=user.id,
            account_id=account.id,
            observed_at=now,
            captured_at=now,
            evidence_json=canonical(evidence),
        )
        db.add(row)
        db.commit()
        return {"id": row.id, **evidence}
    except Exception:
        db.rollback()
        raise


def read_snapshot(db, account, identity):
    row = db.scalar(
        select(LiveBalanceSnapshot).where(
            LiveBalanceSnapshot.id == identity,
            LiveBalanceSnapshot.user_id == account.owner_user_id,
            LiveBalanceSnapshot.account_id == account.id,
        )
    )
    if row is None:
        raise LookupError("Balance snapshot unavailable")
    value = json.loads(row.evidence_json)
    if (
        digest(value) != row.id
        or value["account_id"] != account.id
        or value["user_id"] != account.owner_user_id
    ):
        raise ValueError("SNAPSHOT_INTEGRITY_MISMATCH")
    return value


def snapshot_list(db, account):
    rows = db.scalars(
        select(LiveBalanceSnapshot)
        .where(
            LiveBalanceSnapshot.user_id == account.owner_user_id,
            LiveBalanceSnapshot.account_id == account.id,
        )
        .order_by(LiveBalanceSnapshot.observed_at.desc(), LiveBalanceSnapshot.id)
        .limit(20)
    ).all()
    return [
        {"id": row.id, "observed_at": row.observed_at.replace(tzinfo=UTC).isoformat()}
        for row in rows
    ]


def build_reconciliation_report(db, account, opening_id, closing_id):
    opening, closing = (
        read_snapshot(db, account, identity) for identity in (opening_id, closing_id)
    )
    if opening["wallet_identity"] != closing["wallet_identity"]:
        raise ValueError("WALLET_IDENTITY_CHANGED")
    if (
        opening["observed_at"] >= closing["observed_at"]
        or opening["event_watermark"] > closing["event_watermark"]
    ):
        raise ValueError("INVALID_OBSERVATION_WINDOW")
    end = datetime.fromisoformat(closing["observed_at"])
    events = db.scalars(
        select(AppEvent)
        .where(
            AppEvent.user_id == account.owner_user_id,
            account_events(account),
            AppEvent.source == "live_gateway",
            AppEvent.event_type == "order_fill_confirmed",
            AppEvent.id > opening["event_watermark"],
            AppEvent.id <= closing["event_watermark"],
            AppEvent.occurred_at <= end,
        )
        .order_by(AppEvent.id)
        .limit(501)
    ).all()
    if len(events) > 500:
        raise ValueError("FILL_HISTORY_BOUND_EXCEEDED")
    orders = db.scalars(
        select(LiveOrder)
        .where(
            LiveOrder.user_id == account.owner_user_id,
            account_orders(db, account.owner_user_id, account.id),
        )
        .limit(501)
    ).all()
    if len(orders) > 500:
        raise ValueError("ORDER_HISTORY_BOUND_EXCEEDED")
    order_map = {order.order_hash: order for order in orders}
    fills, reasons = [], []
    for event in events:
        order = order_map.get(event.aggregate_id)
        if order is None:
            reasons.append("FILL_ORDER_PROVENANCE_MISSING")
            continue
        payload = json.loads(event.payload_json)
        instant = payload.get("matched_at")
        if not instant:
            reasons.append("FILL_EXECUTION_TIME_UNVERIFIED")
            continue
        matched_at = datetime.fromisoformat(instant)
        if (
            matched_at.tzinfo is None
            or not datetime.fromisoformat(opening["observed_at"]) < matched_at <= end
        ):
            reasons.append("FILL_OUTSIDE_OBSERVATION_WINDOW")
            continue
        if matched_at > event.occurred_at.replace(tzinfo=UTC):
            reasons.append("FUTURE_FILL_EXECUTION_TIME")
            continue
        fills.append(
            {
                "source_id": event.id,
                "order_hash": order.order_hash,
                "fill_id": payload["trade_id"],
                "transaction_hash": payload["transaction_hash"],
                "token_id": order.token_id,
                "side": order.side,
                "strategy": order.strategy,
                "matched_at": matched_at.isoformat(),
                "shares": payload["shares"],
                "notional": payload["notional"],
                "fee_usdc": payload.get("fee_usdc"),
                "confirmed_rebate_usdc": payload.get("confirmed_rebate_usdc"),
                "estimated_rebate_usdc": payload.get("estimated_rebate_usdc"),
            }
        )
        if order.response.get("reconciliation_required"):
            reasons.append("CURRENT_ORDER_EVIDENCE_QUARANTINED")
    result = reconcile_confirmed_cashflows(fills, opening, closing)
    if opening["active_or_unsettled_orders"] or closing["active_or_unsettled_orders"]:
        reasons.append("ACTIVE_OR_UNSETTLED_ORDERS")
    if reasons:
        result.update(
            status="abstain",
            gross_realized_pnl=None,
            net_realized_pnl=None,
            cash_residual_usdc=None,
        )
        result["reasons"] = sorted(set(result["reasons"] + reasons))
    result.update(
        account_id=account.id,
        opening_snapshot_id=opening_id,
        closing_snapshot_id=closing_id,
        opening_observed_at=opening["observed_at"],
        closing_observed_at=closing["observed_at"],
        input_hash=digest({"opening": opening, "closing": closing, "fills": fills}),
        fill_evidence=fills,
        full_wallet_reconciled=False,
    )
    return json.loads(canonical(result))
