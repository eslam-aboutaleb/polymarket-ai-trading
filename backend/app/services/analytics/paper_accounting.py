"""Durable modeled cash/share accounting shared by paper runners; no signing imports.

The caller owns the outer transaction. Account locks serialize all strategy writers;
orders, journal events and attributable trade rows commit or roll back together.
"""

import json
import logging
import re
from collections import Counter
from decimal import ROUND_DOWN, Decimal, localcontext
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.trading.paper_account import (
    PaperAccount,
    PaperJournalEvent,
    PaperNavPoint,
    PaperOrderRecord,
)
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account
from app.models.users.account_settings import AccountSettings
from app.models.users.user_settings import UserSettings
from app.services.analytics.paper_evidence import EvidenceUnavailable, current_book, final_payout
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT, PaperLedger, positive
from app.services.market_data.replay_capture import aware, digest
from app.services.users.account_service import (
    AccountAccessDenied,
    get_owned_account,
    get_personal_account_read_only,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)
metrics = Counter()
MODEL = "paper-account-IOC-v1"


class PaperPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    max_book_age_seconds: int = Field(default=60, ge=0, le=3600)
    latency_ms: int = Field(default=0, ge=0, le=60_000)
    lot_size: Decimal = Field(default=Decimal("0.01"), gt=0, le=1, max_digits=9, decimal_places=8)


def decimal(value, name="amount", *, zero=False):
    amount = Decimal(str(value))
    positive(amount, name, zero=zero)
    if amount > Decimal("1000000000") or amount.as_tuple().exponent < -8:
        raise ValueError(f"{name} exceeds paper precision/bounds")
    return amount


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
        raise ValueError("invalid paper event identity")
    return value


def account_state(db: Session, user_id: int, account_id: int | None = None) -> PaperAccount:
    owner = (
        get_personal_account_read_only(db, user_id)
        if account_id is None
        else get_owned_account(db, user_id, account_id)
    )
    if owner is None:
        raise AccountAccessDenied("paper account is unavailable")
    db.query(Account).filter_by(id=owner.id, owner_user_id=user_id).with_for_update().one()
    state = db.get(PaperAccount, owner.id)
    if state is None:
        settings = (
            db.query(AccountSettings).filter_by(account_id=owner.id).first()
            if owner.kind == "sub"
            else db.query(UserSettings).filter_by(user_id=user_id).first()
        )
        funding = decimal(
            settings.paper_balance if settings and settings.paper_balance is not None else 1000,
            "funding",
            zero=True,
        )
        legacy = (
            db.query(UserTrade.id)
            .filter(
                UserTrade.user_id == user_id,
                UserTrade.status == "simulated",
                or_(UserTrade.account_id == owner.id, UserTrade.account_id.is_(None))
                if owner.is_default
                else UserTrade.account_id == owner.id,
            )
            .first()
        )
        state = PaperAccount(
            account_id=owner.id,
            user_id=user_id,
            funding=funding,
            legacy_unverified=legacy is not None,
            policy_json=PaperPolicy().model_dump_json(),
            started_at=utc_now(),
        )
        db.add(state)
        db.flush()
    if state.user_id != user_id:
        raise ValueError("paper account owner mismatch")
    return state


def restore(db: Session, state: PaperAccount, as_of=None) -> PaperLedger:
    ledger = PaperLedger(Decimal(state.funding))
    rows = db.query(PaperJournalEvent).filter_by(account_id=state.account_id, user_id=state.user_id)
    if as_of is not None:
        rows = rows.filter(PaperJournalEvent.observed_at <= as_of)
    for row in rows.order_by(PaperJournalEvent.id).yield_per(500):
        event = json.loads(row.payload_json)
        args = event["args"]
        fields = {
            "reserve": (2, 3, 4),
            "fill": (1, 2),
            "cancel": (),
            "settle": (1,),
            "cash_flow": (0,),
        }[event["method"]]
        for index in fields:
            args[index] = Decimal(args[index])
        getattr(ledger, event["method"])(row.event_id, *args)
    return ledger


def book_event(db, state, ledger, event_id, method, *args, observed_at=None):
    body = json.dumps({"method": method, "args": [str(a) for a in args]}, sort_keys=True)
    previous = (
        db.query(PaperJournalEvent)
        .filter_by(account_id=state.account_id, event_id=event_id)
        .first()
    )
    if previous:
        if previous.payload_json != body:
            raise ValueError("paper journal identity conflict")
        return
    getattr(ledger, method)(event_id, *args)
    db.add(
        PaperJournalEvent(
            account_id=state.account_id,
            user_id=state.user_id,
            event_id=event_id,
            observed_at=observed_at or utc_now(),
            payload_json=body,
        )
    )
    db.flush()


def position_rule_versions(db, state, as_of=None):
    balances, versions, conditions = {}, {}, {}
    rows = db.query(PaperOrderRecord).filter_by(account_id=state.account_id, user_id=state.user_id)
    if as_of is not None:
        rows = rows.filter(PaperOrderRecord.observed_at <= as_of)
    with localcontext(ACCOUNTING_CONTEXT):
        for row in rows.order_by(
            PaperOrderRecord.observed_at, PaperOrderRecord.journal_sequence, PaperOrderRecord.id
        ).yield_per(500):
            result = json.loads(row.result_json)
            if result["status"] not in {"filled", "partial"}:
                continue
            token = result["token_id"]
            shares = Decimal(result["shares"]) * (1 if result["side"] == "BUY" else -1)
            balances[token] = balances.get(token, Decimal(0)) + shares
            versions.setdefault(token, set()).add(result["rule_hash"])
            conditions[token] = result["condition_id"]
            if balances[token] == 0:
                versions[token].clear()
    return versions, conditions


def legacy_blocked(state):
    return state.legacy_unverified and state.legacy_acknowledged_at is None


def configure_account(
    db,
    user_id,
    account_id=None,
    *,
    fee_bps=None,
    policy=None,
    acknowledge_legacy=False,
    funding=None,
):
    with db.begin_nested():
        state = account_state(db, user_id, account_id)
        if funding is not None:
            if db.query(PaperJournalEvent.id).filter_by(account_id=state.account_id).first():
                raise ValueError(
                    "funded accounts use explicit cash-flow events, not balance resets"
                )
            if legacy_blocked(state) and not acknowledge_legacy:
                raise ValueError("explicit legacy exclusion acknowledgement required")
            state.funding = decimal(funding, "funding", zero=True)
            state.started_at = utc_now()
        if acknowledge_legacy:
            state.legacy_acknowledged_at = utc_now()
        if fee_bps is not None:
            fee = decimal(fee_bps, "fee_bps", zero=True)
            if fee > 10000:
                raise ValueError("fee assumption exceeds 10000 bps")
            state.fee_bps = fee
        if policy is not None:
            state.policy_json = PaperPolicy.model_validate(policy).model_dump_json()
        db.flush()
        return state


def find_paper_intent(db, user_id, intent_key, account_id=None):
    state = account_state(db, user_id, account_id)
    row = (
        db.query(PaperOrderRecord)
        .filter_by(account_id=state.account_id, user_id=user_id, intent_key=intent_key)
        .first()
    )
    return json.loads(row.result_json) if row else None


def submit_paper_order(
    db,
    *,
    user_id,
    token_id,
    side,
    amount,
    strategy,
    intent_key=None,
    account_id=None,
    units="USDC",
    limit_price=None,
    as_of=None,
    source_identity=None,
    order_type="IOC",
):
    amount = decimal(amount)
    if side.upper() not in {"BUY", "SELL"} or units not in {"USDC", "SHARES"}:
        raise ValueError("invalid paper side/amount units")
    identity(token_id)
    identity(strategy)
    if limit_price is not None:
        limit_price = decimal(limit_price, "limit price")
        if limit_price > 1:
            raise ValueError("limit price exceeds binary payout")
    key = identity(intent_key or str(uuid4()))
    with localcontext(ACCOUNTING_CONTEXT), db.begin_nested():
        if not get_settings().paper_journal_v2_enabled:
            return {
                "order_id": str(uuid4()),
                "status": "unfilled",
                "shares": "0",
                "notional_usdc": "0",
                "fee_usdc": "0",
                "fill_price": None,
                "realized_pnl": "0",
                "reason": "PAPER_JOURNAL_DISABLED",
                "model_version": MODEL,
                "is_simulated": True,
                "strategy": strategy,
                "account_id": account_id,
            }
        state = account_state(db, user_id, account_id)
        request_hash = digest(
            [
                token_id,
                side.upper(),
                str(amount.normalize(ACCOUNTING_CONTEXT)),
                strategy,
                units,
                str(limit_price),
                source_identity,
                order_type,
                as_of.isoformat() if as_of else None,
            ]
        )
        previous = (
            db.query(PaperOrderRecord)
            .filter_by(account_id=state.account_id, intent_key=key)
            .first()
        )
        if previous:
            if previous.request_hash != request_hash:
                raise ValueError("paper intent key reused for different economic request")
            return json.loads(previous.result_json)
        now = utc_now()
        moment = aware(as_of) if as_of else now
        last = (
            db.query(PaperJournalEvent)
            .filter_by(account_id=state.account_id)
            .order_by(PaperJournalEvent.id.desc())
            .first()
        )
        if (
            moment > now
            or moment < aware(state.started_at)
            or (last and moment < aware(last.observed_at))
        ):
            raise ValueError("new paper orders cannot be backdated/future-dated; use replay")
        ledger = restore(db, state)
        policy = PaperPolicy.model_validate_json(state.policy_json)
        result = {
            "order_id": str(uuid4()),
            "model_version": MODEL,
            "account_id": state.account_id,
            "strategy": strategy,
            "token_id": token_id,
            "side": side.upper(),
            "units": units,
            "requested_amount": str(amount),
            "status": "unfilled",
            "shares": "0",
            "notional_usdc": "0",
            "fee_usdc": "0",
            "realized_pnl": "0",
            "fill_price": None,
            "is_simulated": True,
            "source_identity": source_identity,
            "fills": [],
            "quality": "modeled_not_exchange_confirmed",
            "policy": {
                **policy.model_dump(mode="json"),
                "fee_bps": str(state.fee_bps) if state.fee_bps is not None else None,
                "fee_source": "explicit_account_assumption",
                "order_type": "IOC",
                "remainder": "cancel_unfilled",
                "depth": "cumulative_visible_budget_no_inferred_replenishment",
            },
            "decision_at": moment.isoformat(),
            "reason": "MISSING_BOOK",
        }
        try:
            if legacy_blocked(state):
                raise EvidenceUnavailable("LEGACY_HISTORY_REQUIRES_EXPLICIT_FUNDING_EPOCH")
            if state.fee_bps is None:
                raise EvidenceUnavailable("FEES_UNKNOWN")
            if strategy in {"market_maker", "maker"}:
                raise EvidenceUnavailable("MAKER_FLOW_AND_QUEUE_EVIDENCE_REQUIRED")
            if order_type != "IOC":
                raise EvidenceUnavailable("RESTING_ORDER_MODEL_UNAVAILABLE")
            if policy.latency_ms:
                raise EvidenceUnavailable("NO_POST_LATENCY_EXECUTION_EVIDENCE")
            evidence = current_book(db, token_id, moment, max_age=policy.max_book_age_seconds)
            versions, _ = position_rule_versions(db, state)
            if versions.get(token_id) and versions[token_id] != {evidence.rule_hash}:
                raise EvidenceUnavailable("POSITION_RULE_VERSION_CHANGED")
            result.update(
                source_snapshot_id=evidence.capture_id,
                condition_id=evidence.condition_id,
                rule_hash=evidence.rule_hash,
                book_content_id=evidence.book.liquidity_id or evidence.book.snapshot_id,
                source_at=evidence.book.source_at.isoformat(),
                received_at=evidence.book.observed_at.isoformat(),
            )
            levels = evidence.book.asks if side.upper() == "BUY" else evidence.book.bids
            if not levels:
                raise EvidenceUnavailable("MISSING_SIDE_DEPTH")
            if any(level.price % evidence.tick_size != 0 for level in levels):
                raise EvidenceUnavailable("OFF_TICK_BOOK")
            if limit_price is not None and limit_price % evidence.tick_size != 0:
                raise EvidenceUnavailable("OFF_TICK_LIMIT")
            if (
                amount if units == "SHARES" else amount / levels[0].price
            ) < evidence.minimum_shares:
                raise EvidenceUnavailable("BELOW_VENUE_MINIMUM_SIZE")
            consumed = {}
            for previous in (
                db.query(PaperOrderRecord).filter_by(account_id=state.account_id).yield_per(500)
            ):
                data = json.loads(previous.result_json)
                if data.get("book_content_id") == result["book_content_id"]:
                    for fill in data.get("fills", []):
                        consumed[fill["level"]] = consumed.get(fill["level"], Decimal(0)) + Decimal(
                            fill["shares"]
                        )
            remaining = amount
            fees_before, pnl_before = ledger.fees, ledger.realized_pnl
            for index, level in enumerate(levels):
                side = side.upper()
                if limit_price is not None and (
                    level.price > limit_price if side == "BUY" else level.price < limit_price
                ):
                    break
                level_key = f"{side}:{level.price.normalize(ACCOUNTING_CONTEXT)}"
                capacity = max(Decimal(0), level.shares - consumed.get(level_key, Decimal(0)))
                quantity = min(remaining / level.price if units == "USDC" else remaining, capacity)
                quantity = min(
                    quantity,
                    ledger.available_cash / (level.price * (1 + Decimal(state.fee_bps) / 10000))
                    if side == "BUY"
                    else ledger.available_shares(token_id),
                )
                quantity = (quantity / policy.lot_size).to_integral_value(
                    rounding=ROUND_DOWN
                ) * policy.lot_size
                if quantity <= 0:
                    continue
                event_id = f"{result['order_id']}:{index}"
                book_event(
                    db,
                    state,
                    ledger,
                    event_id,
                    "reserve",
                    token_id,
                    side,
                    quantity,
                    level.price,
                    Decimal(state.fee_bps),
                    observed_at=moment,
                )
                book_event(
                    db,
                    state,
                    ledger,
                    event_id + ":fill",
                    "fill",
                    event_id,
                    quantity,
                    level.price,
                    observed_at=moment,
                )
                result["fills"].append(
                    {"level": level_key, "shares": str(quantity), "price": str(level.price)}
                )
                remaining -= quantity * level.price if units == "USDC" else quantity
            shares = sum((Decimal(f["shares"]) for f in result["fills"]), Decimal(0))
            notional = sum(
                (Decimal(f["shares"]) * Decimal(f["price"]) for f in result["fills"]), Decimal(0)
            )
            result.update(
                shares=str(shares),
                notional_usdc=str(notional),
                fee_usdc=str(ledger.fees - fees_before),
                realized_pnl=str(ledger.realized_pnl - pnl_before),
                fill_price=str(notional / shares) if shares else None,
                status="filled"
                if remaining <= Decimal("0.00000001")
                else "partial"
                if shares
                else "unfilled",
                reason="MODELED_NATIVE_DEPTH" if shares else "INSUFFICIENT_CASH_INVENTORY_OR_DEPTH",
                canceled_remainder=str(max(Decimal(0), remaining)),
            )
            if shares:
                trade = UserTrade(
                    user_id=user_id,
                    account_id=state.account_id,
                    market_id=evidence.condition_id,
                    token_id=token_id,
                    action=side.lower(),
                    amount=float(notional),
                    price=float(notional / shares),
                    filled_price=float(notional / shares),
                    filled_size=float(shares),
                    fee_paid=float(ledger.fees - fees_before),
                    status="simulated",
                    strategy_source=strategy,
                    pnl=float(ledger.realized_pnl - pnl_before) if side == "SELL" else None,
                    executed_at=moment,
                    calculation_details=json.dumps(
                        {
                            "paper_journal_order_id": result["order_id"],
                            "model_version": MODEL,
                            "fee_source": "explicit_account_assumption",
                            "capture_id": evidence.capture_id,
                        }
                    ),
                )
                db.add(trade)
                db.flush()
                result["trade_id"] = trade.id
        except EvidenceUnavailable as error:
            result["reason"] = str(error)
        db.add(
            PaperOrderRecord(
                id=result["order_id"],
                account_id=state.account_id,
                user_id=user_id,
                intent_key=key,
                journal_sequence=(
                    db.query(PaperJournalEvent.id)
                    .filter_by(account_id=state.account_id)
                    .order_by(PaperJournalEvent.id.desc())
                    .limit(1)
                    .scalar()
                    or 0
                ),
                request_hash=request_hash,
                result_json=json.dumps(result, sort_keys=True),
                observed_at=moment,
            )
        )
        db.flush()
        metrics[result["reason"]] += 1
        logger.info(
            "paper order account=%s status=%s reason=%s",
            state.account_id,
            result["status"],
            result["reason"],
        )
        return result


def value_account(db, state, as_of=None):
    moment = aware(as_of) if as_of else utc_now()
    if moment < aware(state.started_at):
        raise ValueError("paper account did not exist at requested time")
    ledger = restore(db, state, moment)
    versions, _ = position_rule_versions(db, state, moment)
    policy = PaperPolicy.model_validate_json(state.policy_json)
    marks, provenance, reasons = {}, {}, {}
    for token, position in ledger.positions.items():
        if not position.shares:
            continue
        try:
            evidence = current_book(
                db, token, moment, max_age=policy.max_book_age_seconds, require_size_rules=False
            )
            if versions.get(token) != {evidence.rule_hash}:
                raise EvidenceUnavailable("POSITION_RULE_VERSION_CHANGED")
            bids = [level for level in evidence.book.bids if level.shares > 0]
            if not bids:
                raise EvidenceUnavailable("MISSING_EXIT_BID")
            marks[token] = bids[0].price
            provenance[token] = {
                "capture_id": evidence.capture_id,
                "source_at": evidence.book.source_at.isoformat(),
                "received_at": evidence.book.observed_at.isoformat(),
                "rule_hash": evidence.rule_hash,
                "quality": "indicative_best_bid_not_liquidation_value",
            }
        except EvidenceUnavailable as error:
            reasons[token] = str(error)
    value = ledger.value(marks)
    value.update(
        account_id=state.account_id,
        user_id=state.user_id,
        as_of=moment.isoformat(),
        funding=str(ledger.funding),
        mark_provenance=provenance,
        mark_reasons=reasons,
        fee_bps=str(state.fee_bps) if state.fee_bps is not None else None,
        model_version=MODEL,
        is_paper=True,
        legacy_data_excluded=bool(state.legacy_unverified),
        quality="legacy_unverified"
        if legacy_blocked(state)
        else "incomplete"
        if value["nav"] is None
        else "journal_backed_modeled",
    )
    if legacy_blocked(state):
        for key in ("nav", "net_pnl", "unrealized_pnl", "position_value"):
            value[key] = None
    return value


def capture_nav(db, state, as_of=None):
    moment = aware(as_of) if as_of else utc_now()
    bucket = moment.replace(minute=0, second=0, microsecond=0)
    # Account lock also serializes concurrent snapshot workers.
    db.query(Account).filter_by(
        id=state.account_id, owner_user_id=state.user_id
    ).with_for_update().one()
    existing = (
        db.query(PaperNavPoint).filter_by(account_id=state.account_id, bucket_hour=bucket).first()
    )
    if existing:
        return existing
    value = value_account(db, state, moment)
    row = PaperNavPoint(
        id=str(uuid4()),
        user_id=state.user_id,
        account_id=state.account_id,
        bucket_hour=bucket,
        captured_at=moment,
        valuation_json=json.dumps(value, sort_keys=True),
    )
    db.add(row)
    db.flush()
    metrics["nav_" + value["quality"]] += 1
    return row


def cash_flow(db, user_id, account_id, event_id, amount):
    change = Decimal(str(amount))
    decimal(abs(change), "cash flow")
    with localcontext(ACCOUNTING_CONTEXT), db.begin_nested():
        state = account_state(db, user_id, account_id)
        if legacy_blocked(state):
            raise ValueError("acknowledge exclusion of legacy history before funding a new epoch")
        ledger = restore(db, state)
        book_event(db, state, ledger, "cash:" + identity(event_id), "cash_flow", change)
        return value_account(db, state)


def settle_captured(db, user_id, account_id=None, as_of=None):
    with localcontext(ACCOUNTING_CONTEXT), db.begin_nested():
        state = account_state(db, user_id, account_id)
        if legacy_blocked(state):
            return 0
        moment = aware(as_of) if as_of else utc_now()
        ledger = restore(db, state)
        versions, conditions = position_rule_versions(db, state)
        count = 0
        for token, position in list(ledger.positions.items()):
            if not position.shares or len(versions.get(token, set())) != 1:
                continue
            rule_hash = next(iter(versions[token]))
            try:
                payout = final_payout(db, token, conditions[token], rule_hash, moment)
            except EvidenceUnavailable:
                metrics["settlement_unresolved"] += 1
                continue
            book_event(
                db,
                state,
                ledger,
                "settlement:" + digest([token, rule_hash]),
                "settle",
                token,
                payout,
                observed_at=moment,
            )
            count += 1
        return count
