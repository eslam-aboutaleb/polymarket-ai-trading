"""Account-locked maker lifecycle using the shared journal, never an execution SDK."""

import json
from collections import Counter
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from uuid import uuid4

from app.config import get_settings
from app.models import MarketMakerConfig
from app.models.market_data.venue_observation import VenueObservation
from app.models.trading.paper_account import PaperOrderRecord
from app.models.trading.paper_maker_order import (
    PaperMakerCursor,
    PaperMakerEvidence,
    PaperMakerFill,
    PaperMakerOrder,
)
from app.services.analytics import paper_accounting as accounting
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import BinaryBook, MarketSpec
from app.services.market_data.book_research import ResearchQuery, load_analysis
from app.services.market_data.replay_capture import aware, digest
from app.services.market_data.venue_observations import serialize
from app.services.trading.paper_maker_lifecycle import (
    TERMINAL,
    VERSION,
    FlowEvent,
    MakerOrder,
    ReplayState,
    replay_flow,
)
from app.utils.time import utc_now

metrics = Counter()


def native_flow(row):
    """Do not infer aggressor attribution or continuity from native `side`.

    The archive explicitly labels AGGRESSOR_SEMANTICS_UNVERIFIED. A future
    independently verified adapter can supply the pure model's stronger contract;
    there is intentionally no client-supplied evidence endpoint.
    """
    value = serialize(row)  # verifies canonical receipt/payload hashes
    raw = value["raw"]
    if row.kind != "last_trade_price" or row.event_at is None:
        return None
    metrics["no_aggressor_evidence"] += 1
    return FlowEvent(
        id=row.id,
        token_id=row.token_id,
        condition_id=row.condition_id or "",
        occurred_at=aware(row.event_at),
        available_at=aware(row.received_at),
        price=Decimal(str(raw["price"])),
        shares=Decimal(str(raw["size"])),
        aggressor=None,
        continuous=False,
        evidence_snapshot_id=row.id,
    )


def _rows(db, state):
    return (
        db.query(PaperMakerOrder)
        .filter_by(account_id=state.account_id, user_id=state.user_id)
        .populate_existing()
        .order_by(PaperMakerOrder.created_at, PaperMakerOrder.id)
        .all()
    )


def _save(row, order, moment):
    row.payload_json = order.model_dump_json()
    row.state = order.state
    row.cumulative_filled = order.cumulative_filled
    row.updated_at = moment


def consume(db, state, ledger, events, moment):
    """Caller holds the shared account lock and owns the outer transaction.

    All quotes in this account share a print budget across configurations.
    No commit occurs here; rollback also removes fills, receipts and reservations.
    """
    rows = _rows(db, state)
    cursor = db.get(PaperMakerCursor, state.account_id)
    if cursor is None:
        cursor = PaperMakerCursor(account_id=state.account_id)
        db.add(cursor)
    previous = db.query(PaperMakerEvidence).filter_by(account_id=state.account_id).all()
    replay = ReplayState(
        orders=[
            MakerOrder.model_validate_json(row.payload_json)
            for row in rows
            if row.state not in TERMINAL
        ],
        processed={row.event_id: row.payload_hash for row in previous},
        last_available_at=aware(cursor.available_at) if cursor.available_at else None,
    )
    if not replay.orders:
        return []
    result = replay_flow(replay, events, moment)
    indexed = {row.id: row for row in rows}
    for fill in result.fills:
        row = indexed[fill.order_id]
        order = next(o for o in result.state.orders if o.id == row.id)
        key = f"maker-fill:{digest([fill.order_id, fill.event_id, 0])}"
        before = ledger.realized_pnl
        accounting.book_event(
            db,
            state,
            ledger,
            key,
            "fill",
            f"maker-reserve:{row.id}",
            fill.shares,
            fill.price,
            observed_at=moment,
        )
        db.add(
            PaperMakerFill(
                id=str(uuid4()),
                order_id=row.id,
                event_id=fill.event_id,
                fill_sequence=0,
                realized_pnl_usdc=str(ledger.realized_pnl - before),
                payload_json=fill.model_dump_json(),
                observed_at=moment,
            )
        )
        # Compatible read-side attribution for marks/rule reconciliation; these
        # records are modeled and never inserted as actual UserTrade fills.
        payload = {
            "status": "filled",
            "strategy": "market_maker",
            "condition_id": order.condition_id,
            "token_id": order.token_id,
            "side": order.side,
            "shares": str(fill.shares),
            "notional_usdc": str(fill.shares * fill.price),
            "fee_usdc": str(fill.fee_usdc),
            "realized_pnl": str(ledger.realized_pnl - before),
            "rule_hash": order.rule_hash,
            "model": VERSION,
            "quality": fill.confidence,
            "source_identity": fill.source_snapshot_id,
        }
        db.add(
            PaperOrderRecord(
                id=str(uuid4()),
                account_id=state.account_id,
                user_id=state.user_id,
                intent_key=key,
                journal_sequence=len(ledger.journal),
                request_hash=digest(payload),
                result_json=json.dumps(payload),
                observed_at=moment,
            )
        )
        metrics["modeled_fill_events"] += 1
    for order in result.state.orders:
        if order.state in TERMINAL or order.state == "UNKNOWN_DATA":
            reserve = f"maker-reserve:{order.id}"
            if reserve in ledger.orders:
                accounting.book_event(
                    db,
                    state,
                    ledger,
                    f"maker-release:{order.id}",
                    "cancel",
                    reserve,
                    observed_at=moment,
                )
        _save(indexed[order.id], order, moment)
    known = {row.event_id for row in previous}
    for key, value in result.state.processed.items():
        if key not in known:
            db.add(
                PaperMakerEvidence(
                    account_id=state.account_id,
                    event_id=key,
                    payload_hash=value,
                    payload_json=next(e.model_dump_json() for e in events if e.id == key),
                )
            )
    cursor.available_at = result.state.last_available_at
    for reason in result.reasons:
        metrics[reason.lower()] += 1
    db.flush()
    return result.fills


def _consume_archive(db, state, ledger, moment):
    orders = _rows(db, state)
    active = [row for row in orders if row.state not in TERMINAL]
    if not active:
        return
    oldest = min(aware(row.created_at) for row in active)
    tokens = {MakerOrder.model_validate_json(row.payload_json).token_id for row in active}
    rows = (
        db.query(VenueObservation)
        .filter(
            VenueObservation.token_id.in_(tokens),
            VenueObservation.received_at >= oldest,
            VenueObservation.received_at <= moment,
        )
        .order_by(VenueObservation.received_at, VenueObservation.id)
        .limit(10001)
        .all()
    )
    if len(rows) > 10000:
        raise ValueError("maker evidence backlog requires reconciliation")
    events = []
    for row in rows:
        event = native_flow(row)
        if event is not None:
            events.append(event)
    consume(db, state, ledger, events, moment)


def sync(db, config, expected, moment=None):
    settings = get_settings()
    if (
        not settings.paper_journal_v2_enabled
        or not settings.binary_book_research_enabled
        or not settings.observation_archive_enabled
    ):
        raise ValueError("maker lifecycle requires shared journal and captured books")
    moment = aware(moment or utc_now())
    with accounting.transaction(db), localcontext(ACCOUNTING_CONTEXT):
        state = accounting.account_state(db, config.user_id, config.account_id)
        # Lock order is always account -> maker config, matching all writers.
        db.query(MarketMakerConfig).filter_by(
            id=config.id, user_id=state.user_id
        ).with_for_update().one()
        ledger = accounting.restore(db, state)
        classify_legacy(db, state)
        if accounting.legacy_blocked(state):
            raise ValueError("legacy funding epoch acknowledgement required")
        if accounting.settlement_issues(db, state, ledger, moment):
            raise ValueError("settlement reconciliation required")
        analysis, _ = load_analysis(
            db, ResearchQuery(condition_id=config.condition_id, as_of=moment)
        )
        spec = MarketSpec.model_validate(analysis["spec"]) if analysis.get("spec") else None
        if spec and (spec.yes_token != config.token_id_yes or spec.no_token != config.token_id_no):
            raise ValueError("maker token mapping conflict")
        if (
            spec
            and spec.active
            and spec.supported
            and all(value["grade"] == "VERIFIED_BOOK" for value in analysis["books"].values())
        ):
            _consume_archive(db, state, ledger, moment)
        else:
            cancel_config(db, config, moment)
        capture_drift(db, state, moment)
        rows = _rows(db, state)
        current = [r for r in rows if r.config_id == config.id and r.state not in TERMINAL]
        desired = {}
        reasons = []
        for quote in expected:
            if quote["token_id"] not in (config.token_id_yes, config.token_id_no) or quote[
                "side"
            ] not in {"BUY", "SELL"}:
                raise ValueError("maker token/side mismatch")
            price, notional = Decimal(str(quote["price"])), Decimal(str(quote["size"]))
            if (
                not price.is_finite()
                or not notional.is_finite()
                or not 0 < price < 1
                or not 0 < notional <= Decimal("1000000000")
            ):
                raise ValueError("invalid maker price/notional")
            if spec is None or not spec.active or not spec.supported:
                reasons.append("MISSING_OR_HALTED_MARKET")
                continue
            book = BinaryBook.model_validate(
                analysis["books"]["YES" if quote["token_id"] == spec.yes_token else "NO"]
            )
            if book.grade != "VERIFIED_BOOK":
                reasons.append("STALE_OR_INSUFFICIENT_BOOK")
                continue
            # Generated floats are definitions: reject off-tick values, never
            # silently change an explicitly quoted price.
            if price % spec.tick:
                reasons.append("OFF_TICK_PRICE")
                continue
            shares = (notional / price / spec.lot).to_integral_value(rounding=ROUND_DOWN) * spec.lot
            if shares < spec.minimum_shares or shares > Decimal("1000000000"):
                reasons.append("MINIMUM_OR_SIZE_CONSTRAINT")
                continue
            key = digest(
                [
                    quote["token_id"],
                    quote["side"],
                    str(price.normalize()),
                    str(shares.normalize()),
                    spec.rule_hash,
                ]
            )
            desired[key] = (quote, price, shares, book, spec.rule_hash)
        cancelled = created = 0
        for row in current:
            order = MakerOrder.model_validate_json(row.payload_json)
            if row.quote_key not in desired or order.state == "UNKNOWN_DATA":
                effective = order.cancel_effective_at or (
                    moment + timedelta(milliseconds=settings.paper_maker_latency_ms)
                )
                if order.state == "UNKNOWN_DATA":
                    effective = moment  # gaps never retain modeled eligibility
                order = order.model_copy(
                    update={
                        "state": "CANCELLED" if effective <= moment else "CANCEL_REQUESTED",
                        "cancel_effective_at": effective,
                    }
                )
                reserve = f"maker-reserve:{row.id}"
                if effective <= moment and reserve in ledger.orders:
                    accounting.book_event(
                        db,
                        state,
                        ledger,
                        f"maker-release:{row.id}",
                        "cancel",
                        reserve,
                        observed_at=moment,
                    )
                _save(row, order, moment)
                cancelled += 1
        active_keys = {r.quote_key for r in current if r.state not in TERMINAL}
        for key, (quote, price, shares, book, rule_hash) in desired.items():
            if key in active_keys:
                continue
            deployed = sum(
                Decimal(row.shares - row.cumulative_filled) * Decimal(row.limit_price)
                for row in _rows(db, state)
                if row.config_id == config.id and row.state not in TERMINAL
            )
            if deployed + price * shares > Decimal(str(config.max_collateral)):
                reasons.append("CONFIG_COLLATERAL_CAP")
                continue
            if price * shares < Decimal(str(config.min_order_size)):
                reasons.append("MINIMUM_NOTIONAL")
                continue
            if state.fee_bps is None:
                reasons.append("UNKNOWN_FEE_POLICY")
                continue
            identifier = str(uuid4())
            order = MakerOrder(
                id=identifier,
                token_id=quote["token_id"],
                condition_id=config.condition_id,
                side=quote["side"],
                limit_price=price,
                shares=shares,
                active_from=moment + timedelta(milliseconds=settings.paper_maker_latency_ms),
                expires_at=moment
                + timedelta(
                    milliseconds=settings.paper_maker_latency_ms,
                    seconds=settings.paper_maker_expiry_seconds,
                ),
                source_snapshot_id=next(
                    identity
                    for identity in reversed(book.source_ids)
                    if db.get(VenueObservation, identity).kind == "book"
                ),
                rule_hash=rule_hash,
                queue_ahead=settings.paper_maker_queue_ahead,
                fee_bps=state.fee_bps,
                fee_version="explicit-account-flat-bps-v1",
                state="RESTING",
            )
            reserve_key = f"maker-reserve:{identifier}"
            try:
                # Ledger rejects overspend/short inventory before any row is added.
                accounting.book_event(
                    db,
                    state,
                    ledger,
                    reserve_key,
                    "reserve",
                    order.token_id,
                    order.side,
                    shares,
                    price,
                    state.fee_bps,
                    observed_at=moment,
                )
            except ValueError:
                reasons.append("INSUFFICIENT_CASH_OR_INVENTORY")
                continue
            db.add(
                PaperMakerOrder(
                    id=identifier,
                    config_id=config.id,
                    user_id=state.user_id,
                    account_id=state.account_id,
                    quote_key=key,
                    source_snapshot_id=order.source_snapshot_id,
                    metadata_snapshot_id=spec.metadata_id,
                    payload_json=order.model_dump_json(),
                    state=order.state,
                    shares=shares,
                    limit_price=price,
                    cumulative_filled=0,
                    created_at=moment,
                    updated_at=moment,
                )
            )
            created += 1
        db.flush()
        metrics["quote_intents"] += created
        for reason in reasons:
            metrics[reason.lower()] += 1
        config.current_open_orders = len([r for r in current if r.state not in TERMINAL]) + created
        return {
            "intent_count": len(desired),
            "intents_created": created,
            "intents_withdrawn": cancelled,
            "policy": VERSION,
            "fill_status": "modeled_only_requires_attributed_flow_and_queue",
            "reasons": sorted(set(reasons)),
        }


def cancel_config(db, config, moment=None):
    moment = aware(moment or utc_now())
    with accounting.transaction(db):
        state = accounting.account_state(db, config.user_id, config.account_id)
        ledger = accounting.restore(db, state)
        for row in _rows(db, state):
            if row.config_id != config.id or row.state in TERMINAL:
                continue
            order = MakerOrder.model_validate_json(row.payload_json)
            reserve = f"maker-reserve:{row.id}"
            if reserve in ledger.orders:
                accounting.book_event(
                    db,
                    state,
                    ledger,
                    f"maker-release:{row.id}",
                    "cancel",
                    reserve,
                    observed_at=moment,
                )
            _save(
                row,
                order.model_copy(update={"state": "CANCELLED", "cancel_effective_at": moment}),
                moment,
            )
        config.current_open_orders = 0
        db.flush()


def report(db, config, account):
    from app.models import UserTrade

    rows = (
        db.query(PaperMakerOrder)
        .filter_by(config_id=config.id, account_id=account.id, user_id=account.owner_user_id)
        .order_by(PaperMakerOrder.created_at.desc(), PaperMakerOrder.id)
        .limit(100)
        .all()
    )
    values = []
    with localcontext(ACCOUNTING_CONTEXT):
        for row in rows:
            order = MakerOrder.model_validate_json(row.payload_json)
            remaining = (
                order.shares - order.cumulative_filled
                if row.state not in TERMINAL and row.state != "UNKNOWN_DATA"
                else Decimal(0)
            )
            values.append(
                {
                    **order.model_dump(mode="json"),
                    "created_at": aware(row.created_at).isoformat(),
                    "updated_at": aware(row.updated_at).isoformat(),
                    "notional_usdc": str(order.shares * order.limit_price),
                    "executed_notional_usdc": str(order.cumulative_filled * order.limit_price),
                    "reserved_cash_usdc": str(
                        remaining * order.limit_price * (1 + order.fee_bps / 10000)
                    )
                    if order.side == "BUY" and order.fee_bps is not None
                    else "0",
                    "reserved_shares": str(remaining) if order.side == "SELL" else "0",
                    "confidence": "modeled_low_confidence"
                    if order.cumulative_filled
                    else "unfilled_no_confirmed_execution",
                }
            )
        quoted = sum((Decimal(v["notional_usdc"]) for v in values), Decimal(0))
        executed = sum((Decimal(v["executed_notional_usdc"]) for v in values), Decimal(0))
    fills = (
        db.query(PaperMakerFill).filter(PaperMakerFill.order_id.in_([row.id for row in rows])).all()
    )
    with localcontext(ACCOUNTING_CONTEXT):
        realized = sum((Decimal(row.realized_pnl_usdc) for row in fills), Decimal(0))
        fees = sum((Decimal(json.loads(row.payload_json)["fee_usdc"]) for row in fills), Decimal(0))
    legacy = db.query(UserTrade).filter(
        UserTrade.user_id == account.owner_user_id,
        UserTrade.status == "simulated",
        UserTrade.strategy_source == "market_maker",
    )
    if account.is_default:
        from sqlalchemy import or_

        legacy = legacy.filter(
            or_(UserTrade.account_id == account.id, UserTrade.account_id.is_(None))
        )
    else:
        legacy = legacy.filter(UserTrade.account_id == account.id)
    return {
        "policy": VERSION,
        "is_paper": True,
        "config_enabled": bool(config.enabled),
        "fill_status": "modeled_only_requires_attributed_flow_and_queue",
        "intents": values,
        "realized_pnl_usdc": str(realized),
        "cost_adjusted_spread_capture_usdc": str(realized),
        "fees_usdc": str(fees),
        "unrealized_pnl_usdc": None,
        "unrealized_reason": "Use shared account NAV: token inventory is shared across strategies",
        "quoted_notional_usdc": str(quoted),
        "executed_notional_usdc": str(executed),
        "modeled_fill_fraction": str(executed / quoted) if quoted else None,
        "legacy_history_quality": "legacy_unverified_fill",
        "legacy_trade_count": legacy.count(),
        "legacy_excluded_from_decision_grade": True,
        "post_fill_horizons_seconds": [5, 30, 60],
        "markouts": _markouts(db, rows),
        "shadow_generation_only_notional_usdc": str(quoted),
        "metrics": {
            "quote_intents": len(values),
            "modeled_filled_orders": sum(Decimal(v["cumulative_filled"]) > 0 for v in values),
            "unknown_data_orders": sum(v["state"] == "UNKNOWN_DATA" for v in values),
            "cancelled_orders": sum(v["state"] == "CANCELLED" for v in values),
        },
        "window": "latest_100_orders",
        "realized_pnl_source": "shared_paper_journal",
        "unrealized_pnl_source": "fresh_captured_exit_bid_or_unknown",
    }


def capture_drift(db, state, moment):
    """Freeze indicative markouts at 5/30/60 seconds; never realized profits.

    Bounded to 20 fills per sync. Late backfills cannot revise frozen outcomes.
    A missing, stale or pre-fill source mark remains explicitly unavailable.
    """
    from app.models.trading.paper_maker_order import PaperMakerMark

    rows = (
        db.query(PaperMakerFill, PaperMakerOrder)
        .join(PaperMakerOrder, PaperMakerOrder.id == PaperMakerFill.order_id)
        .filter(
            PaperMakerOrder.account_id == state.account_id, PaperMakerOrder.user_id == state.user_id
        )
        .order_by(PaperMakerFill.observed_at.desc())
        .limit(20)
        .all()
    )
    with localcontext(ACCOUNTING_CONTEXT):
        for fill, row in rows:
            order = MakerOrder.model_validate_json(row.payload_json)
            for horizon in (5, 30, 60):
                target = aware(fill.observed_at) + timedelta(seconds=horizon)
                if target > moment or db.get(PaperMakerMark, (fill.id, horizon)):
                    continue
                analysis, _ = load_analysis(
                    db,
                    ResearchQuery(condition_id=order.condition_id, as_of=target, max_age_seconds=5),
                )
                label = (
                    "YES"
                    if (analysis.get("spec") or {}).get("yes_token") == order.token_id
                    else "NO"
                )
                raw = analysis.get("books", {}).get(label)
                result = {
                    "as_of": target.isoformat(),
                    "horizon_seconds": horizon,
                    "markout_per_share": None,
                    "reason": "MISSING_STALE_OR_PREFILL_MARK",
                    "quality": "indicative_midpoint_not_realized_pnl",
                }
                if raw:
                    book = BinaryBook.model_validate(raw)
                    if (
                        book.grade == "VERIFIED_BOOK"
                        and book.age_seconds is not None
                        and target - timedelta(seconds=float(book.age_seconds))
                        >= aware(fill.observed_at)
                    ):
                        midpoint = (book.bids[0].price + book.asks[0].price) / 2
                        result.update(
                            markout_per_share=str(
                                (midpoint - order.limit_price) * (1 if order.side == "BUY" else -1)
                            ),
                            source_ids=book.source_ids,
                            book_hash=book.book_hash,
                            reason=None,
                        )
                db.add(
                    PaperMakerMark(
                        fill_id=fill.id, horizon_seconds=horizon, payload_json=json.dumps(result)
                    )
                )
    db.flush()


def _markouts(db, orders):
    from app.models.trading.paper_maker_order import PaperMakerMark

    if not orders:
        return []
    rows = (
        db.query(PaperMakerMark)
        .join(PaperMakerFill, PaperMakerFill.id == PaperMakerMark.fill_id)
        .filter(PaperMakerFill.order_id.in_([row.id for row in orders]))
        .limit(300)
        .all()
    )
    return [json.loads(row.payload_json) for row in rows]


def classify_legacy(db, state, limit=500):
    """Bounded immutable sidecar backfill. Original trade rows are untouched."""
    from sqlalchemy import or_

    from app.models import Account, PaperMakerLegacy, UserTrade

    owner = db.get(Account, state.account_id)
    rows = (
        db.query(UserTrade)
        .filter(
            UserTrade.user_id == state.user_id,
            UserTrade.status == "simulated",
            UserTrade.strategy_source == "market_maker",
            or_(UserTrade.account_id == state.account_id, UserTrade.account_id.is_(None))
            if owner.is_default
            else UserTrade.account_id == state.account_id,
            ~db.query(PaperMakerLegacy).filter(PaperMakerLegacy.trade_id == UserTrade.id).exists(),
        )
        .order_by(UserTrade.id)
        .limit(max(1, min(limit, 1000)))
        .all()
    )
    for row in rows:
        body = {
            name: str(getattr(row, name))
            for name in (
                "id",
                "amount",
                "price",
                "status",
                "strategy_source",
                "calculation_details",
            )
        }
        db.add(
            PaperMakerLegacy(
                trade_id=row.id, quality="legacy_unverified_fill", original_hash=digest(body)
            )
        )
    db.flush()
    return len(rows)
