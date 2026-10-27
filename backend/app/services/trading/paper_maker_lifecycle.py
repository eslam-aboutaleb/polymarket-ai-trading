"""Pure maker eligibility replay. Outputs are modeled proposals, never booked fills.

Only explicitly attributed flow and an explicit queue assumption can consume a
quote. Native prints without that evidence must not be upgraded by adapters.
Persistence must atomically store replay state and shared-ledger fill events.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.services.analytics.replay.ledger import accounting
from app.services.market_data.binary_book import Contract

VERSION = "maker-attributed-flow-v2"
TERMINAL = {"FILLED", "CANCELLED", "EXPIRED"}


class MakerOrder(Contract):
    id: str = Field(min_length=1, max_length=200)
    token_id: str = Field(min_length=1, max_length=200)
    condition_id: str = Field(min_length=1, max_length=200)
    rule_hash: str = "unverified"
    side: Literal["BUY", "SELL"]
    limit_price: Decimal = Field(gt=0, lt=1, decimal_places=18)
    shares: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=18)
    active_from: AwareDatetime
    expires_at: AwareDatetime
    source_snapshot_id: str = Field(min_length=1, max_length=200)
    queue_ahead: Decimal | None = Field(None, ge=0, le=1_000_000_000, decimal_places=18)
    fee_bps: Decimal | None = Field(None, ge=0, le=10000, decimal_places=8)
    fee_version: str | None = None
    cumulative_filled: Decimal = Field(Decimal(0), ge=0)
    state: Literal[
        "CREATED",
        "RESTING",
        "PARTIAL",
        "FILLED",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "EXPIRED",
        "UNKNOWN_DATA",
    ] = "CREATED"
    cancel_effective_at: AwareDatetime | None = None
    policy: Literal["maker-attributed-flow-v2"] = VERSION

    @model_validator(mode="after")
    def valid_window(self):
        if self.expires_at <= self.active_from or self.cumulative_filled > self.shares:
            raise ValueError("invalid order window or cumulative quantity")
        if not self.source_snapshot_id:
            raise ValueError("source snapshot required")
        return self


class FlowEvent(Contract):
    id: str = Field(min_length=1, max_length=200)
    token_id: str = Field(min_length=1, max_length=200)
    condition_id: str = Field(min_length=1, max_length=200)
    occurred_at: AwareDatetime
    available_at: AwareDatetime
    price: Decimal = Field(gt=0, lt=1, decimal_places=18)
    shares: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=18)
    aggressor: Literal["BUY", "SELL"] | None = None
    evidence_snapshot_id: str = Field(min_length=1, max_length=200)
    continuous: bool = False

    @model_validator(mode="after")
    def clocks(self):
        if self.occurred_at > self.available_at or not self.evidence_snapshot_id:
            raise ValueError("flow clock/provenance invalid")
        return self


class ModeledFill(Contract):
    order_id: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    shares: Decimal
    price: Decimal
    fee_usdc: Decimal
    source_snapshot_id: str = Field(min_length=1, max_length=200)
    confidence: Literal["modeled_low_confidence"] = "modeled_low_confidence"
    classification: Literal["eligible_fill"] = "eligible_fill"


class ReplayState(Contract):
    orders: list[MakerOrder] = Field(max_length=1000)
    # Store hashes as well as IDs: a changed duplicate is a data conflict.
    processed: dict[str, str] = Field(default_factory=dict)
    last_available_at: AwareDatetime | None = None


class ReplayResult(Contract):
    state: ReplayState
    fills: list[ModeledFill]
    reasons: list[str]


@accounting
def replay_flow(state: ReplayState, events: list[FlowEvent], as_of, max_delay_seconds=60):
    """Deterministic receipt-order replay with one shared volume budget per print.

    Frozen state is not mutated, so a caller can roll back the entire proposal.
    Unknown continuity freezes eligibility; explicit order replacement is required
    to establish a new snapshot and queue assumption after a gap.
    """
    import hashlib

    if as_of.tzinfo is None or not 0 <= max_delay_seconds <= 300:
        raise ValueError("aware cutoff and bounded delay required")
    if len(events) > 10000 or (state.last_available_at and as_of < state.last_available_at):
        raise ValueError("bounded batch and forward replay cutoff required")
    orders = {o.id: o for o in state.orders}
    if len(orders) != len(state.orders):
        raise ValueError("duplicate order identity")
    processed = dict(state.processed)
    last = state.last_available_at
    fills, reasons = [], []
    for event in sorted(events, key=lambda e: (e.available_at, e.id)):
        if event.available_at > as_of:
            continue
        digest = hashlib.sha256(event.model_dump_json().encode()).hexdigest()
        if event.id in processed:
            if processed[event.id] != digest:
                raise ValueError("conflicting flow identity")
            reasons.append("DUPLICATE_EVENT")
            continue
        processed[event.id] = digest
        reordered = last is not None and event.available_at <= last
        last = max(last, event.available_at) if last else event.available_at
        budget = event.shares
        for order in sorted(orders.values(), key=lambda o: (o.active_from, o.id)):
            if order.state in TERMINAL or order.state == "UNKNOWN_DATA":
                continue
            if (order.token_id, order.condition_id) != (event.token_id, event.condition_id):
                continue
            if event.occurred_at < order.active_from or event.available_at < order.active_from:
                continue
            if event.available_at >= order.expires_at:
                orders[order.id] = order.model_copy(update={"state": "EXPIRED"})
                continue
            if order.cancel_effective_at and event.available_at >= order.cancel_effective_at:
                orders[order.id] = order.model_copy(update={"state": "CANCELLED"})
                continue
            if (
                reordered
                or not event.continuous
                or event.available_at - event.occurred_at > timedelta(seconds=max_delay_seconds)
            ):
                orders[order.id] = order.model_copy(update={"state": "UNKNOWN_DATA"})
                reasons.append("FLOW_GAP_REORDER_OR_STALE")
                continue
            if event.aggressor is None or order.queue_ahead is None:
                reasons.append("MISSING_AGGRESSOR_OR_QUEUE")
                continue
            if order.fee_bps is None or not order.fee_version:
                reasons.append("UNKNOWN_FEE_POLICY")
                continue
            touched = (
                order.side == "BUY"
                and event.aggressor == "SELL"
                and event.price <= order.limit_price
            ) or (
                order.side == "SELL"
                and event.aggressor == "BUY"
                and event.price >= order.limit_price
            )
            if not touched:
                continue
            queue_used = min(budget, order.queue_ahead)
            budget -= queue_used
            queue = order.queue_ahead - queue_used
            quantity = min(budget, order.shares - order.cumulative_filled)
            total = order.cumulative_filled + quantity
            orders[order.id] = order.model_copy(
                update={
                    "queue_ahead": queue,
                    "cumulative_filled": total,
                    "state": "FILLED"
                    if total == order.shares
                    else "PARTIAL"
                    if total
                    else "RESTING",
                }
            )
            if quantity:
                budget -= quantity
                fills.append(
                    ModeledFill(
                        order_id=order.id,
                        event_id=event.id,
                        shares=quantity,
                        price=order.limit_price,
                        fee_usdc=quantity * order.limit_price * order.fee_bps / Decimal(10000),
                        source_snapshot_id=event.evidence_snapshot_id,
                    )
                )
    for key, order in orders.items():
        if order.state not in TERMINAL:
            if as_of >= order.expires_at:
                orders[key] = order.model_copy(update={"state": "EXPIRED"})
            elif order.cancel_effective_at and as_of >= order.cancel_effective_at:
                orders[key] = order.model_copy(update={"state": "CANCELLED"})
            elif order.state == "CREATED" and as_of >= order.active_from:
                orders[key] = order.model_copy(update={"state": "RESTING"})
    return ReplayResult(
        state=ReplayState(
            orders=list(orders.values()), processed=processed, last_available_at=last
        ),
        fills=fills,
        reasons=sorted(set(reasons)),
    )
