"""Deterministic research-only binary book reconstruction. No execution facilities."""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT

VERSION = "binary-book-v1"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Level(Contract):
    price: Decimal = Field(gt=0, lt=1, decimal_places=18, max_digits=20)
    shares: Decimal = Field(ge=0, le=1_000_000_000_000, decimal_places=18, max_digits=32)


class MarketSpec(Contract):
    condition_id: str = Field(min_length=1, max_length=200)
    yes_token: str = Field(min_length=1, max_length=120)
    no_token: str = Field(min_length=1, max_length=120)
    rule_hash: str = Field(min_length=1, max_length=64)
    metadata_id: str = Field(min_length=1, max_length=64)
    received_at: AwareDatetime
    active: bool
    tick: Decimal = Field(gt=0, le=Decimal("0.1"))
    minimum_shares: Decimal = Field(gt=0, le=1_000_000_000)
    lot: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)
    supported: bool = True

    @model_validator(mode="after")
    def validate_identity(self):
        if self.yes_token == self.no_token or not self.yes_token or not self.no_token:
            raise ValueError("distinct YES/NO token mapping required")
        if not self.rule_hash or not self.metadata_id:
            raise ValueError("rule and metadata provenance required")
        if self.tick not in map(Decimal, ("0.1", "0.01", "0.001", "0.0001")):
            raise ValueError("unsupported venue tick")
        return self


class BookEvent(Contract):
    snapshot_id: str
    token_id: str
    condition_id: str
    rule_hash: str
    received_at: AwareDatetime
    source_at: AwareDatetime | None = None
    connection_id: str
    kind: Literal["snapshot", "delta", "disconnect", "tick_change", "trade"]
    sequence: int | None = None
    sequence_contract: Literal["unavailable", "consecutive"] = "unavailable"
    checksum: Literal["unavailable", "verified", "mismatch", "incomplete"] = "unavailable"
    bids: list[Level] = Field(default_factory=list, max_length=1000)
    asks: list[Level] = Field(default_factory=list, max_length=1000)
    changes: list[tuple[Literal["BUY", "SELL"], Level]] = Field(
        default_factory=list, max_length=1000
    )


class BinaryBook(Contract):
    version: Literal["binary-book-v1"] = VERSION
    condition_id: str
    token_id: str
    rule_hash: str
    as_of: AwareDatetime
    grade: Literal["VERIFIED_BOOK", "PARTIAL", "STALE", "NO_HISTORY"]
    reasons: list[str]
    source_ids: list[str]
    book_hash: str | None = None
    age_seconds: Decimal | None = None
    bids: list[Level] = Field(default_factory=list)
    asks: list[Level] = Field(default_factory=list)
    needs_resnapshot: bool = True
    expires_at: AwareDatetime | None = None
    confidence: str = "captured_displayed_depth_only"
    continuous_history_verified: bool = False


def _reconstruct(
    spec: MarketSpec,
    token_id: str,
    events: list[BookEvent],
    as_of: datetime,
    max_age_seconds: int = 60,
) -> BinaryBook:
    from app.services.market_data.replay_capture import digest

    if as_of.tzinfo is None or not 1 <= max_age_seconds <= 300:
        raise ValueError("aware cutoff and bounded freshness required")
    reasons, source_ids = [], []
    bids, asks = {}, {}
    last_source = None
    last_sequence = None
    connection = None
    usable = False
    seen = {}
    continuous = False
    gap = False
    last_receipt = None
    last_contract = None
    ordered = sorted(
        events,
        key=lambda e: (
            e.received_at,
            e.sequence if e.sequence_contract == "consecutive" and e.sequence is not None else -1,
            e.snapshot_id,
        ),
    )
    for event in ordered:
        if event.received_at > as_of:
            continue
        identity = event.model_dump_json()
        if event.snapshot_id in seen:
            if seen[event.snapshot_id] != identity:
                raise ValueError("EVENT_IDENTITY_CONFLICT")
            continue
        seen[event.snapshot_id] = identity
        if event.token_id != token_id:
            continue
        source_ids.append(event.snapshot_id)
        if event.kind != "trade":
            if event.received_at == last_receipt and (
                event.sequence_contract == "unavailable" or last_contract == "unavailable"
            ):
                reasons, gap = ["AMBIGUOUS_RECEIPT_ORDER"], True
                continue
            last_receipt, last_contract = event.received_at, event.sequence_contract
        if event.condition_id != spec.condition_id or event.rule_hash != spec.rule_hash:
            reasons = ["TOKEN_CONDITION_OR_RULE_MISMATCH"]
            gap = True
            continue
        if event.received_at < spec.received_at:
            reasons = ["BOOK_PRECEDES_METADATA"]
            gap = True
            continue
        if event.kind == "trade":
            continue  # Trade prints do not prove remaining book depth or queue fills.
        if event.kind in ("disconnect", "tick_change"):
            reasons = ["DISCONNECTED" if event.kind == "disconnect" else "TICK_RULE_CHANGED"]
            gap = True
            continue
        if event.source_at is None or event.source_at > event.received_at:
            reasons = ["SOURCE_CLOCK_UNKNOWN_OR_FUTURE"]
            gap = True
            continue
        if event.checksum in ("mismatch", "incomplete"):
            reasons = [
                "CHECKSUM_MISMATCH" if event.checksum == "mismatch" else "CHECKSUM_UNVERIFIABLE"
            ]
            gap = True
            continue
        if last_source and event.source_at < last_source:
            reasons = ["OUT_OF_ORDER_SOURCE_EVENT"]
            gap = True
            continue
        if (
            event.sequence_contract == "consecutive"
            and event.sequence is not None
            and last_sequence is not None
            and connection == event.connection_id
            and event.sequence <= last_sequence
        ):
            reasons, gap = ["SEQUENCE_REORDER_OR_CONFLICT"], True
            continue
        if event.kind == "snapshot":
            bids = {v.price: v.shares for v in event.bids if v.shares > 0}
            asks = {v.price: v.shares for v in event.asks if v.shares > 0}
            if len({v.price for v in event.bids}) != len(event.bids) or len(
                {v.price for v in event.asks}
            ) != len(event.asks):
                reasons, gap = ["DUPLICATE_PRICE_LEVEL"], True
                continue
            reasons = [] if event.checksum == "verified" else ["VENUE_CHECKSUM_UNAVAILABLE"]
            usable, gap = True, False
            continuous = event.sequence_contract == "consecutive" and event.sequence is not None
            if not continuous:
                reasons.append("SEQUENCE_UNAVAILABLE_SNAPSHOT_ONLY")
        else:
            if (
                not usable
                or gap
                or connection != event.connection_id
                or event.sequence_contract != "consecutive"
                or event.sequence is None
                or last_sequence is None
                or event.sequence != last_sequence + 1
            ):
                reasons, gap = ["DELTA_CONTINUITY_UNPROVEN"], True
                continue
            if len({(side, level.price) for side, level in event.changes}) != len(event.changes):
                reasons, gap = ["AMBIGUOUS_DELTA_LEVEL"], True
                continue
            for side, level in event.changes:
                levels = bids if side == "BUY" else asks
                if level.shares == 0:
                    levels.pop(level.price, None)
                else:
                    levels[level.price] = level.shares
        last_source, last_sequence, connection = (
            event.source_at,
            event.sequence,
            event.connection_id,
        )
        if any(price % spec.tick for price in [*bids, *asks]):
            reasons, gap = ["OFF_TICK_LEVEL"], True
        if not bids or not asks:
            reasons, gap = ["EMPTY_BOOK_SIDE"], True
        elif max(bids) >= min(asks):
            reasons, gap = ["CROSSED_BOOK"], True
    age = Decimal(str((as_of - last_source).total_seconds())) if last_source else None
    grade = "NO_HISTORY" if not source_ids else "PARTIAL"
    if not spec.supported:
        reasons = ["UNSUPPORTED_MARKET_FORM"]
    elif not spec.active:
        reasons = ["HALTED_OR_CLOSED_MARKET"]
    elif spec.received_at > as_of:
        reasons = ["METADATA_AFTER_CUTOFF"]
    elif (as_of - spec.received_at).total_seconds() > 300:
        grade, reasons = "STALE", ["STALE_MARKET_METADATA"]
    elif age is not None and age > max_age_seconds:
        grade, reasons = "STALE", [*reasons, "STALE_BOOK"]
    elif usable and not gap:
        grade = "VERIFIED_BOOK"
    levels_bids = [Level(price=p, shares=bids[p]) for p in sorted(bids, reverse=True)]
    levels_asks = [Level(price=p, shares=asks[p]) for p in sorted(asks)]
    fingerprint = (
        digest(
            {
                "token_id": token_id,
                "condition_id": spec.condition_id,
                "rule_hash": spec.rule_hash,
                "version": VERSION,
                "bids": [(str(v.price), str(v.shares)) for v in levels_bids],
                "asks": [(str(v.price), str(v.shares)) for v in levels_asks],
            }
        )
        if usable
        else None
    )
    return BinaryBook(
        condition_id=spec.condition_id,
        token_id=token_id,
        rule_hash=spec.rule_hash,
        as_of=as_of,
        grade=grade,
        reasons=reasons
        or (
            ["NO_CAPTURE"]
            if not source_ids
            else (["NO_BOOK_SNAPSHOT"] if grade != "VERIFIED_BOOK" else [])
        ),
        source_ids=source_ids,
        book_hash=fingerprint,
        age_seconds=age,
        bids=levels_bids if grade == "VERIFIED_BOOK" else [],
        asks=levels_asks if grade == "VERIFIED_BOOK" else [],
        needs_resnapshot=grade != "VERIFIED_BOOK",
        expires_at=last_source + timedelta(seconds=max_age_seconds)
        if grade == "VERIFIED_BOOK"
        else None,
        continuous_history_verified=continuous and grade == "VERIFIED_BOOK",
    )


def reconstruct(
    spec: MarketSpec,
    token_id: str,
    events: list[BookEvent],
    as_of: datetime,
    max_age_seconds: int = 60,
) -> BinaryBook:
    with localcontext(ACCOUNTING_CONTEXT):
        if token_id not in (spec.yes_token, spec.no_token):
            raise ValueError("TOKEN_NOT_IN_BINARY_MAPPING")
        return _reconstruct(spec, token_id, events, as_of, max_age_seconds)


class WalkRequest(Contract):
    side: Literal["BUY", "SELL"]
    units: Literal["SHARES", "USDC"] = "SHARES"
    amount: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=8)
    fee_bps: Decimal | None = Field(default=None, ge=0, le=10000)
    fee_version: str | None = Field(default=None, min_length=1, max_length=80)
    limit_price: Decimal | None = Field(default=None, gt=0, lt=1)


class DepthPoint(Contract):
    price: Decimal
    shares: Decimal
    cumulative_shares: Decimal
    cumulative_gross: Decimal


class CostEstimate(Contract):
    version: Literal["binary-book-v1"] = VERSION
    status: Literal["modeled", "abstain"]
    reason: str | None = None
    token_id: str
    book_hash: str | None
    requested: WalkRequest
    matched_shares: Decimal = Decimal(0)
    gross_notional: Decimal | None = None
    fee: Decimal | None = None
    net_cash: Decimal | None = None
    vwap: Decimal | None = None
    coverage: Decimal = Decimal(0)
    partial: bool = False
    displayed_capacity: Decimal | None = None
    curve: list[DepthPoint] = Field(default_factory=list)
    confidence: str = "counterfactual_displayed_depth_only"
    assumptions: list[str] = Field(
        default_factory=lambda: [
            "flat_notional_fee_assumption",
            "model_share_precision_not_venue_lot_rule",
            "no_queue_or_fill_probability",
            "no_measured_price_impact_claim",
            "no_liquidity_replenishment_assumed",
        ]
    )
    measured_price_impact: None = None
    fill_probability: None = None


def walk_depth(spec: MarketSpec, book: BinaryBook, request: WalkRequest) -> CostEstimate:
    from decimal import ROUND_DOWN

    def abstain(reason):
        return CostEstimate(
            status="abstain",
            reason=reason,
            token_id=book.token_id,
            book_hash=book.book_hash,
            requested=request,
        )

    with localcontext(ACCOUNTING_CONTEXT):
        if book.grade != "VERIFIED_BOOK":
            return abstain("BOOK_NOT_USABLE")
        if (
            book.token_id not in (spec.yes_token, spec.no_token)
            or book.rule_hash != spec.rule_hash
            or book.condition_id != spec.condition_id
        ):
            return abstain("BOOK_IDENTITY_MISMATCH")
        if request.fee_bps is None or not request.fee_version:
            return abstain("FEE_POLICY_UNKNOWN")
        if request.limit_price is not None and request.limit_price % spec.tick:
            return abstain("LIMIT_OFF_TICK")
        fee_rate = request.fee_bps / Decimal(10000)
        cash_factor = 1 + fee_rate if request.side == "BUY" else 1 - fee_rate
        if cash_factor <= 0:
            return abstain("FEES_EXCEED_PROCEEDS")
        levels = book.asks if request.side == "BUY" else book.bids
        capacity = sum((level.shares for level in levels), Decimal(0))
        qty, gross = Decimal(0), Decimal(0)
        points = []
        requested_qty = (request.amount / spec.lot).to_integral_value(
            rounding=ROUND_DOWN
        ) * spec.lot
        if request.units == "SHARES" and requested_qty < spec.minimum_shares:
            return abstain("BELOW_MINIMUM_ORDER_SIZE")
        for level in levels:
            if request.limit_price is not None and (
                (request.side == "BUY" and level.price > request.limit_price)
                or (request.side == "SELL" and level.price < request.limit_price)
            ):
                break
            remaining = (
                (requested_qty - qty)
                if request.units == "SHARES"
                else ((request.amount - gross * cash_factor) / (level.price * cash_factor))
            )
            take = (min(level.shares, remaining) / spec.lot).to_integral_value(
                rounding=ROUND_DOWN
            ) * spec.lot
            if take <= 0:
                continue
            qty += take
            gross += take * level.price
            points.append(
                DepthPoint(
                    price=level.price, shares=take, cumulative_shares=qty, cumulative_gross=gross
                )
            )
        if qty < spec.minimum_shares:
            return abstain("INSUFFICIENT_ELIGIBLE_DEPTH_OR_MINIMUM_SIZE")
        fee = gross * fee_rate
        net = gross + fee if request.side == "BUY" else gross - fee
        coverage = qty / request.amount if request.units == "SHARES" else net / request.amount
        return CostEstimate(
            status="modeled",
            token_id=book.token_id,
            book_hash=book.book_hash,
            requested=request,
            matched_shares=qty,
            gross_notional=gross,
            fee=fee,
            net_cash=net,
            vwap=gross / qty,
            coverage=min(coverage, Decimal(1)),
            partial=coverage < 1,
            displayed_capacity=capacity,
            curve=points,
        )


def complement_summary(yes: BinaryBook, no: BinaryBook) -> dict:
    """Independent displayed inequalities, never fungible/netted liquidity."""
    result = {
        "netting_permitted": False,
        "conversion_cost": None,
        "arbitrage_claim": False,
        "ask_sum": None,
        "bid_sum": None,
        "flags": [],
    }
    if yes.grade != "VERIFIED_BOOK" or no.grade != "VERIFIED_BOOK":
        result["flags"] = ["BOTH_TOKEN_BOOKS_REQUIRED"]
        return result
    if (
        yes.condition_id != no.condition_id
        or yes.rule_hash != no.rule_hash
        or yes.token_id == no.token_id
    ):
        result["flags"] = ["TOKEN_PAIR_IDENTITY_MISMATCH"]
        return result
    if yes.age_seconds is None or no.age_seconds is None:
        result["flags"] = ["TOKEN_BOOK_AGE_UNAVAILABLE"]
        return result
    with localcontext(ACCOUNTING_CONTEXT):
        skew = abs(yes.age_seconds - no.age_seconds)
    if skew > 5:
        result["flags"] = ["ASYNCHRONOUS_TOKEN_BOOKS"]
        return result
    with localcontext(ACCOUNTING_CONTEXT):
        ask_sum = yes.asks[0].price + no.asks[0].price
        bid_sum = yes.bids[0].price + no.bids[0].price
        result.update(ask_sum=str(ask_sum), bid_sum=str(bid_sum))
        if ask_sum < 1:
            result["flags"].append("COMPLEMENT_ASK_INEQUALITY_NO_CONVERSION_PROOF")
        if bid_sum > 1:
            result["flags"].append("COMPLEMENT_BID_INEQUALITY_NO_CONVERSION_PROOF")
    return result


def round_trip_cost(spec: MarketSpec, book: BinaryBook, request: WalkRequest) -> dict:
    """Same-token displayed spread/fee cost; no conversion or actual execution claim."""
    unavailable = {
        "status": "abstain",
        "entry_cost": None,
        "exit_proceeds": None,
        "modeled_spread_and_fee_cost": None,
        "actual_execution": False,
    }
    if request.units != "SHARES":
        return {**unavailable, "reason": "SHARE_REQUEST_REQUIRED"}
    buy = walk_depth(spec, book, request.model_copy(update={"side": "BUY"}))
    sell = walk_depth(spec, book, request.model_copy(update={"side": "SELL"}))
    if buy.status != "modeled" or sell.status != "modeled":
        return {**unavailable, "reason": "BOTH_SIDES_AND_FEES_REQUIRED"}
    with localcontext(ACCOUNTING_CONTEXT):
        common = min(buy.matched_shares, sell.matched_shares)
        buy = walk_depth(spec, book, request.model_copy(update={"side": "BUY", "amount": common}))
        sell = walk_depth(spec, book, request.model_copy(update={"side": "SELL", "amount": common}))
        return {
            "status": "modeled",
            "matched_shares": str(common),
            "requested_shares": str(request.amount),
            "entry_cost": str(buy.net_cash),
            "exit_proceeds": str(sell.net_cash),
            "modeled_spread_and_fee_cost": str(buy.net_cash - sell.net_cash),
            "partial": common < request.amount,
            "actual_execution": False,
        }


class QueueAssessment(Contract):
    status: Literal["unavailable"] = "unavailable"
    reason: str = "NO_VERIFIED_AGGRESSOR_FLOW_OR_QUEUE_POSITION"
    estimated_fill_shares: None = None
    fill_probability: None = None
    actual_execution: Literal[False] = False


class NoQueueEvidencePolicy:
    """Queue-policy boundary for #34; an order-book touch cannot prove a maker fill."""

    def assess(self, book: BinaryBook) -> QueueAssessment:  # noqa: ARG002
        return QueueAssessment()
