"""Account-scoped native balance observations and confirmed-fill read side (#3).

No signing, submission, balance credits or UserTrade/P&L writes. Known-token
coverage and missing fees/finality remain explicit, never invented earnings.
"""

import hashlib
import json
import logging
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, or_, select

from app.models.analytics.live_balance_snapshot import LiveBalanceSnapshot
from app.models.platform.app_event import AppEvent
from app.models.trading.live_order import LiveOrder
from app.models.trading.user_trade import UserTrade
from app.models.users.user import User
from app.security.credential_store import load_wallet_credentials
from app.services.analytics.attribution_contracts import ConfirmedFillPayload
from app.services.analytics.native_wallet_evidence import wallet_evidence
from app.services.analytics.pnl_reconciliation import reconcile_confirmed_cashflows
from app.services.market_data.replay_capture import aware
from app.services.trading.execution import clob_transport
from app.services.trading.execution.order_state import account_orders
from app.utils.time import utc_now

VERSION = "live-attribution-v2"
logger = logging.getLogger(__name__)
metrics = Counter()


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
        previous = db.scalar(
            select(LiveBalanceSnapshot)
            .where(
                LiveBalanceSnapshot.user_id == user.id,
                LiveBalanceSnapshot.account_id == account.id,
            )
            .order_by(LiveBalanceSnapshot.observed_at.desc())
            .limit(1)
        )
        inventory = wallet_evidence(
            str(stored.get("funder") or user.wallet_address),
            start=aware(previous.observed_at).isoformat() if previous else None,
            end=started_at.isoformat(),
            deadline=deadline,
        )
        tokens = sorted(set(tokens) | set(inventory["positions"]))
        if len(tokens) > 20:
            raise ValueError("KNOWN_TOKEN_OBSERVATION_BOUND_EXCEEDED")
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
        if any(
            Decimal(quantity) != Decimal(positions.get(token, "0"))
            for token, quantity in inventory["positions"].items()
        ):
            raise ValueError("WALLET_INDEXER_BALANCE_MISMATCH")
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
            "coverage": "venue_indexed_wallet_tokens",
            "wallet_evidence": inventory,
            "collateral_unit": "native_clob_collateral",
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
    return [{"id": row.id, "observed_at": aware(row.observed_at).isoformat()} for row in rows]


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
            AppEvent.event_type.in_(("order_fill_confirmed", "order_intent_created")),
            AppEvent.id <= closing["event_watermark"],
            AppEvent.occurred_at <= end,
        )
        .order_by(AppEvent.id)
        .limit(5001)
    ).all()
    if len(events) > 5000:
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
    from app.services.analytics.attribution_benchmarks import (
        benchmark_fill,
        mark_observation,
        settlement_observation,
    )

    fills, history, reasons, benchmarks = [], [], [], []
    intents, fill_identities = {}, {}
    for event in events:
        if event.event_type == "order_intent_created":
            if event.aggregate_id in intents:
                raise ValueError("CONFLICTING_ORDER_INTENT_EVIDENCE")
            intents[event.aggregate_id] = event
    for event in events:
        if event.event_type != "order_fill_confirmed":
            continue
        order = order_map.get(event.aggregate_id)
        if order is None:
            reasons.append("FILL_ORDER_PROVENANCE_MISSING")
            continue
        intent = intents.get(event.aggregate_id)
        if intent is None or intent.id >= event.id:
            reasons.append("IMMUTABLE_ORDER_INTENT_MISSING")
            continue
        try:
            identity = json.loads(intent.payload_json)
            payload = json.loads(event.payload_json)
            if not isinstance(identity, dict) or not isinstance(payload, dict):
                raise ValueError("INVALID_AUDIT_OBJECT")
        except (ValueError, TypeError):
            reasons.append("MALFORMED_AUDIT_EVIDENCE")
            continue
        if not identity.get("condition_id"):
            reasons.append("ORDER_CONDITION_PROVENANCE_MISSING")
            continue
        if any(
            identity.get(key) != getattr(order, key) for key in ("token_id", "side", "strategy")
        ):
            reasons.append("ORDER_INTENT_IDENTITY_CHANGED")
            continue
        instant = payload.get("matched_at")
        if not instant:
            reasons.append("FILL_EXECUTION_TIME_UNVERIFIED")
            continue
        try:
            validated = ConfirmedFillPayload.model_validate(payload)
            payload = validated.model_dump(mode="json")
            matched_at = validated.matched_at
        except ValueError:
            reasons.append("MALFORMED_CONFIRMED_FILL")
            continue
        if matched_at.tzinfo is None or matched_at > end or matched_at < aware(intent.occurred_at):
            reasons.append("FILL_OUTSIDE_OBSERVATION_WINDOW")
            continue
        if matched_at > aware(event.occurred_at):
            reasons.append("FUTURE_FILL_EXECUTION_TIME")
            continue
        fill = {
            "source_id": event.id,
            "intent_source_id": intent.id,
            "condition_id": identity["condition_id"],
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
        economic_identity = (fill["order_hash"], fill["fill_id"])
        content = {key: value for key, value in fill.items() if key != "source_id"}
        if economic_identity in fill_identities:
            if fill_identities[economic_identity] != content:
                raise ValueError("CONFLICTING_FILL_IDENTITY")
            continue
        fill_identities[economic_identity] = content
        if matched_at <= datetime.fromisoformat(opening["observed_at"]):
            if event.id > opening["event_watermark"]:
                reasons.append("LATE_CONFIRMED_OPENING_FILL")
            history.append(fill)
        else:
            if event.id <= opening["event_watermark"]:
                reasons.append("OPENING_WATERMARK_TIME_CONFLICT")
            fills.append(fill)
            benchmarks.append(
                benchmark_fill(
                    db,
                    SimpleNamespace(
                        token_id=order.token_id,
                        side=order.side,
                        created_at=aware(intent.occurred_at),
                    ),
                    fill,
                    condition_id=identity["condition_id"],
                )
            )
        if order.response.get("reconciliation_required"):
            reasons.append("CURRENT_ORDER_EVIDENCE_QUARANTINED")
    empty = {"cash_usdc": "0", "positions": {}, "coverage": "complete_wallet"}
    past = reconcile_confirmed_cashflows(history, empty, empty)
    opening_lots = past["closing_lots"]
    try:
        result = reconcile_confirmed_cashflows(fills, opening, closing, opening_lots=opening_lots)
        if "UNMATCHED_SELL_QUANTITY" in past["reasons"]:
            reasons.append("OPENING_HISTORY_UNMATCHED_SELL")
    except ValueError as exc:
        if str(exc) != "OPENING_LOT_QUANTITY_MISMATCH":
            raise
        opening_lots = {}
        reasons.append("OPENING_LOT_QUANTITY_MISMATCH")
        result = reconcile_confirmed_cashflows(fills, opening, closing)
    from app.services.analytics.attribution_flows import external_flows

    flows = external_flows(opening, closing, fills)
    opening_mark = mark_observation(db, opening, opening_lots)
    closing_mark = mark_observation(db, closing, result["closing_lots"])
    settlement = settlement_observation(db, closing)
    mark_change = (
        closing_mark["unrealized_pnl"] - opening_mark["unrealized_pnl"]
        if all(mark["unrealized_pnl"] is not None for mark in (opening_mark, closing_mark))
        else None
    )
    benchmark_totals = {
        key: sum((row[key] for row in benchmarks), Decimal(0))
        if all(row[key] is not None for row in benchmarks)
        else None
        for key in ("implementation_shortfall", "spread_cost", "execution_difference")
    }
    result.update(
        **benchmark_totals,
        model_costs=None,
        forecast_attribution=None,
        timing_attribution=None,
        sizing_attribution=None,
        observed_cash_residual_after_indexed_flows=(
            result["cash_residual_usdc"]
            - flows["funding_cashflow"]
            - flows["unattributed_rebates"]
            - flows["other_income"]
            if result["cash_residual_usdc"] is not None
            and all(
                flows[name] is not None
                for name in ("funding_cashflow", "unattributed_rebates", "other_income")
            )
            else None
        ),
        external_flows=flows,
        opening_mark=opening_mark,
        closing_mark=closing_mark,
        unrealized_pnl=closing_mark["unrealized_pnl"],
        mark_contribution=mark_change,
        benchmarks=benchmarks,
        benchmark_version="arrival-midpoint-spread-v1",
        settlement_evidence=settlement,
        history_evidence=history,
        opening_lots=opening_lots,
        history_source_ids=past["source_ids"],
        confidence={
            "classification": "diagnostic_only",
            "profitability_claim": False,
            "comparable_sample_count": sum(
                b["status"] == "observed_comparison" for b in benchmarks
            ),
            "forward_event_count": 0,
            "confidence_interval": None,
            "release_decision": "ABSTAIN_NO_FORWARD_VALIDATION",
        },
    )
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
        algorithm_version="fifo-attribution-v2.1",
        fill_set_version=digest({"history": history, "period": fills}),
        account_id=account.id,
        opening_snapshot_id=opening_id,
        closing_snapshot_id=closing_id,
        opening_observed_at=opening["observed_at"],
        closing_observed_at=closing["observed_at"],
        input_hash=digest(
            {
                "opening": opening,
                "closing": closing,
                "fills": fills,
                "history": history,
                "benchmarks": benchmarks,
                "settlement_evidence": settlement,
                "external_flows": flows,
                "opening_mark": opening_mark,
                "closing_mark": closing_mark,
                "reasons": result["reasons"],
                "version": VERSION,
            }
        ),
        fill_evidence=fills,
        full_wallet_reconciled=False,
    )
    from app.services.analytics.attribution_contracts import acceptance_checks

    result["acceptance"] = acceptance_checks(result)
    result["acceptance_complete"] = all(item["status"] == "pass" for item in result["acceptance"])
    result["verified_net_realized_pnl"] = (
        result["net_realized_pnl"] if result["acceptance_complete"] else None
    )
    metrics["report_" + result["status"]] += 1
    for reason in result["reasons"]:
        metrics["reason_" + reason] += 1
    logger.info(
        "live_attribution_report",
        extra={
            "account_id": account.id,
            "input_hash": result["input_hash"],
            "status": result["status"],
            "fill_count": len(fills),
            "history_count": len(history),
            "reason_codes": result["reasons"],
        },
    )
    return json.loads(canonical(result))
