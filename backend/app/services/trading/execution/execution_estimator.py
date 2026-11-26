"""Read-only pre-trade execution cost estimate and implementation shortfall (#7).

Pure and side-effect free: no network, no database, no signing and no order action.
Prices come from a validated ``BinaryBook`` (issue #18) and every number is a
counterfactual walk of *displayed* depth, never a promise of a fill. Missing or
stale inputs abstain instead of defaulting to zero.
"""

import hashlib
import json
import math
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import (
    BinaryBook,
    Contract,
    MarketSpec,
    NoQueueEvidencePolicy,
    WalkRequest,
    walk_depth,
)

VERSION = "execution-estimate-v1"
BPS = Decimal(10000)

Mode = Literal["MARKETABLE_LIMIT", "FOK", "MAKER_REST"]


class EstimateRequest(Contract):
    order_intent_id: str = Field(min_length=1, max_length=80)
    mode: Mode = "MARKETABLE_LIMIT"
    side: Literal["BUY", "SELL"]
    shares: Decimal = Field(gt=0, le=1_000_000_000, decimal_places=8)
    # Worst acceptable price. For taker modes it bounds the walk; for MAKER_REST it is
    # the resting price. Taker modes without any bound are rejected, never unbounded.
    limit_price: Decimal | None = Field(default=None, gt=0, lt=1)
    max_slippage_bps: Decimal | None = Field(default=None, ge=0, le=10000)
    fee_bps: Decimal | None = Field(default=None, ge=0, le=10000)
    fee_version: str | None = Field(default=None, min_length=1, max_length=80)
    maker_fee_bps: Decimal | None = Field(default=None, ge=0, le=10000)


class ExecutionEstimate(Contract):
    version: Literal["execution-estimate-v1"] = VERSION
    estimate_id: str | None = None
    status: Literal["modeled", "rejected", "abstain"]
    reason: str | None = None
    order_intent_id: str
    mode: Mode
    side: Literal["BUY", "SELL"]
    token_id: str
    condition_id: str
    book_snapshot_id: str | None = None
    benchmark_at: AwareDatetime | None = None
    book_age_ms: int | None = None
    quantity: Decimal
    fillable_quantity: Decimal = Decimal(0)
    residual_quantity: Decimal | None = None
    fill_status: Literal["FULL", "PARTIAL", "NONE", "UNMODELED"] = "NONE"
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    arrival_mid: Decimal | None = None
    executable_vwap: Decimal | None = None
    gross_notional: Decimal | None = None
    fees: Decimal | None = None
    net_cash: Decimal | None = None
    depth_coverage: Decimal = Decimal(0)
    expected_price_impact: Decimal | None = None
    price_impact_basis: str = "displayed_depth_walk_not_measured"
    expected_cost_vs_mid_bps: Decimal | None = None
    quote_expiry: AwareDatetime | None = None
    max_acceptable_price: Decimal | None = None
    confidence: str = "counterfactual_displayed_depth_only"
    fill_probability: None = None
    nonfill_cost: None = None
    adverse_selection_cost: None = None
    maker_queue: dict | None = None
    live_order_action: Literal[False] = False
    assumptions: list[str] = Field(
        default_factory=lambda: [
            "displayed_depth_only_no_liquidity_replenishment",
            "flat_notional_fee_assumption",
            "no_queue_or_fill_probability",
            "estimate_is_not_a_fill_and_may_expire_before_submission",
        ]
    )

    @model_validator(mode="after")
    def seal(self):
        if self.estimate_id is None:
            body = self.model_dump(mode="json", exclude={"estimate_id"})
            object.__setattr__(
                self,
                "estimate_id",
                hashlib.sha256(
                    json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            )
        return self


def _blank(request: EstimateRequest, book: BinaryBook, status, reason, **extra):
    return ExecutionEstimate(
        status=status,
        reason=reason,
        order_intent_id=request.order_intent_id,
        mode=request.mode,
        side=request.side,
        token_id=book.token_id,
        condition_id=book.condition_id,
        quantity=request.shares,
        **extra,
    )


def _round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return (price / tick).to_integral_value(rounding=rounding) * tick


def book_age_ms(book: BinaryBook, now: datetime) -> int | None:
    """Age of the newest source event at ``now`` (cutoff-to-now plus age at cutoff)."""
    if book.age_seconds is None:
        return None
    seconds = (now - book.as_of).total_seconds() + float(book.age_seconds)
    return max(0, math.ceil(seconds * 1000))


def estimate_execution(
    spec: MarketSpec, book: BinaryBook, request: EstimateRequest, now: datetime
) -> ExecutionEstimate:
    """Walk the displayed book for ``request``; never prices missing depth as a fill."""
    if now.tzinfo is None:
        raise ValueError("aware evaluation time required")
    with localcontext(ACCOUNTING_CONTEXT):
        if book.grade != "VERIFIED_BOOK" or not book.bids or not book.asks:
            return _blank(request, book, "abstain", "BOOK_NOT_USABLE")
        if (
            book.token_id not in (spec.yes_token, spec.no_token)
            or book.rule_hash != spec.rule_hash
            or book.condition_id != spec.condition_id
        ):
            return _blank(request, book, "abstain", "BOOK_IDENTITY_MISMATCH")
        if book.expires_at is None or now > book.expires_at or now < book.as_of:
            return _blank(request, book, "abstain", "QUOTE_EXPIRED_OR_FROM_FUTURE")
        best_bid, best_ask = book.bids[0].price, book.asks[0].price
        arrival = {
            "book_snapshot_id": book.book_hash,
            "benchmark_at": book.as_of,
            "book_age_ms": book_age_ms(book, now),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "arrival_mid": (best_bid + best_ask) / 2,
            "quote_expiry": book.expires_at,
        }
        # Venue rules are enforced before any pricing: reject, do not round to fit.
        if request.shares % spec.lot:
            return _blank(request, book, "rejected", "REJECT_OFF_LOT_SIZE", **arrival)
        if request.shares < spec.minimum_shares:
            return _blank(request, book, "rejected", "REJECT_BELOW_MINIMUM_ORDER_SIZE", **arrival)
        if request.limit_price is not None and request.limit_price % spec.tick:
            return _blank(request, book, "rejected", "REJECT_OFF_TICK_PRICE", **arrival)
        if request.mode == "MAKER_REST":
            return _maker(book, request, arrival)
        return _taker(spec, book, request, arrival)


def _taker(spec, book, request, arrival):
    best = arrival["best_ask"] if request.side == "BUY" else arrival["best_bid"]
    ceiling = request.limit_price
    if request.max_slippage_bps is not None:
        factor = request.max_slippage_bps / BPS
        bound = _round_to_tick(
            best * (1 + factor) if request.side == "BUY" else best * (1 - factor),
            spec.tick,
            up=request.side == "SELL",
        ).quantize(spec.tick)
        # Tightest of the explicit limit and the slippage bound.
        if ceiling is None or (bound < ceiling if request.side == "BUY" else bound > ceiling):
            ceiling = bound
    if ceiling is None:
        return _blank(request, book, "rejected", "REJECT_MAX_ACCEPTABLE_PRICE_REQUIRED", **arrival)
    if not 0 < ceiling < 1:
        return _blank(
            request, book, "rejected", "REJECT_MAX_ACCEPTABLE_PRICE_OUT_OF_RANGE", **arrival
        )
    walk = walk_depth(
        spec,
        book,
        WalkRequest(
            side=request.side,
            units="SHARES",
            amount=request.shares,
            fee_bps=request.fee_bps,
            fee_version=request.fee_version,
            limit_price=ceiling,
        ),
    )
    arrival = {**arrival, "max_acceptable_price": ceiling}
    if walk.status == "abstain":
        if walk.reason == "INSUFFICIENT_ELIGIBLE_DEPTH_OR_MINIMUM_SIZE":
            return _blank(
                request,
                book,
                "modeled",
                "NO_DISPLAYED_DEPTH_WITHIN_MAX_ACCEPTABLE_PRICE",
                residual_quantity=request.shares,
                fill_status="NONE",
                **arrival,
            )
        return _blank(request, book, "abstain", walk.reason, **arrival)
    full = walk.matched_shares == request.shares
    if request.mode == "FOK" and not full:
        # Fill-or-kill: a partial walk would be killed, so it is never priced as a fill.
        return _blank(
            request,
            book,
            "modeled",
            "FOK_WOULD_NOT_FILL_INSUFFICIENT_DEPTH_WITHIN_LIMIT",
            residual_quantity=request.shares,
            depth_coverage=walk.matched_shares / request.shares,
            fill_status="NONE",
            **arrival,
        )
    mid = arrival["arrival_mid"]
    impact = (
        walk.vwap - arrival["best_ask"]
        if request.side == "BUY"
        else arrival["best_bid"] - walk.vwap
    )
    cost_bps = (
        (walk.vwap - mid) / mid * BPS if request.side == "BUY" else (mid - walk.vwap) / mid * BPS
    )
    return _blank(
        request,
        book,
        "modeled",
        None if full else "PARTIAL_DEPTH_WITHIN_MAX_ACCEPTABLE_PRICE",
        fillable_quantity=walk.matched_shares,
        residual_quantity=request.shares - walk.matched_shares,
        fill_status="FULL" if full else "PARTIAL",
        executable_vwap=walk.vwap,
        gross_notional=walk.gross_notional,
        fees=walk.fee,
        net_cash=walk.net_cash,
        depth_coverage=walk.matched_shares / request.shares,
        expected_price_impact=impact,
        expected_cost_vs_mid_bps=cost_bps,
        **arrival,
    )


def _maker(book, request, arrival):
    if request.limit_price is None:
        return _blank(request, book, "rejected", "REJECT_MAKER_PRICE_REQUIRED", **arrival)
    if request.maker_fee_bps is None or not request.fee_version:
        return _blank(request, book, "abstain", "MAKER_FEE_POLICY_UNKNOWN", **arrival)
    crosses = (
        request.limit_price >= arrival["best_ask"]
        if request.side == "BUY"
        else request.limit_price <= arrival["best_bid"]
    )
    if crosses:
        # A crossing price is marketable (taker), not a rest: refuse to call it a maker.
        return _blank(request, book, "rejected", "REJECT_WOULD_CROSS_SPREAD_AS_MAKER", **arrival)
    gross = request.limit_price * request.shares
    fee = gross * request.maker_fee_bps / BPS
    mid = arrival["arrival_mid"]
    cost_bps = (
        (request.limit_price - mid) / mid * BPS
        if request.side == "BUY"
        else (mid - request.limit_price) / mid * BPS
    )
    return _blank(
        request,
        book,
        "modeled",
        "MAKER_FILL_UNMODELED_NO_QUEUE_EVIDENCE",
        fill_status="UNMODELED",
        executable_vwap=request.limit_price,
        gross_notional=gross,
        fees=fee,
        net_cash=gross + fee if request.side == "BUY" else gross - fee,
        expected_price_impact=Decimal(0),
        expected_cost_vs_mid_bps=cost_bps,
        max_acceptable_price=request.limit_price,
        maker_queue=NoQueueEvidencePolicy().assess(book).model_dump(mode="json"),
        **arrival,
    )


# ── Staleness: a changed quote invalidates a prior estimate ──────────────


class Revalidation(Contract):
    valid: bool
    reasons: list[str]
    estimate_id: str | None
    current_book_snapshot_id: str | None


def revalidate_estimate(
    prior: ExecutionEstimate, current: BinaryBook | None, now: datetime
) -> Revalidation:
    """An estimate is current only while the same book hash is live and unexpired."""
    reasons = []
    if prior.status != "modeled":
        reasons.append("PRIOR_ESTIMATE_NOT_MODELED")
    if prior.quote_expiry is None or now > prior.quote_expiry:
        reasons.append("QUOTE_EXPIRED")
    if current is None or current.grade != "VERIFIED_BOOK" or not current.bids or not current.asks:
        reasons.append("CURRENT_BOOK_NOT_USABLE")
    else:
        if current.token_id != prior.token_id or current.condition_id != prior.condition_id:
            reasons.append("CURRENT_BOOK_IDENTITY_MISMATCH")
        if (current.bids[0].price, current.asks[0].price) != (prior.best_bid, prior.best_ask):
            reasons.append("BEST_QUOTE_CHANGED")
        if current.book_hash != prior.book_snapshot_id:
            reasons.append("BOOK_DEPTH_CHANGED")
    return Revalidation(
        valid=not reasons,
        reasons=reasons,
        estimate_id=prior.estimate_id,
        current_book_snapshot_id=current.book_hash if current is not None else None,
    )


# ── Implementation shortfall against one shared benchmark timestamp ─────


class ShortfallInput(Contract):
    side: Literal["BUY", "SELL"]
    ordered_shares: Decimal = Field(gt=0)
    filled_shares: Decimal = Field(ge=0)
    fill_vwap: Decimal | None = Field(default=None, gt=0, lt=1)
    fees_paid: Decimal | None = Field(default=None, ge=0)
    arrival_mid: Decimal = Field(gt=0, lt=1)
    benchmark_at: AwareDatetime
    # Optional later reference price (observed, never forecast) for missed-trade cost.
    later_mid: Decimal | None = Field(default=None, gt=0, lt=1)
    later_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.filled_shares > self.ordered_shares:
            raise ValueError("filled exceeds ordered")
        if self.filled_shares > 0 and self.fill_vwap is None:
            raise ValueError("fill price required for a filled quantity")
        if self.later_at is not None and self.later_at < self.benchmark_at:
            raise ValueError("later reference precedes benchmark")
        if (self.later_mid is None) != (self.later_at is None):
            raise ValueError("later reference needs both price and time")
        return self


class Shortfall(Contract):
    status: Literal["measured", "abstain"]
    reason: str | None = None
    benchmark_at: AwareDatetime | None = None
    arrival_mid: Decimal | None = None
    fill_rate: Decimal | None = None
    execution_cost: Decimal | None = None
    fee_cost: Decimal | None = None
    missed_trade_cost: Decimal | None = None
    total_cost: Decimal | None = None
    price_shortfall_bps: Decimal | None = None
    total_shortfall_bps: Decimal | None = None
    components_observed: list[str] = Field(default_factory=list)
    components_missing: list[str] = Field(default_factory=list)


def implementation_shortfall(value: ShortfallInput) -> Shortfall:
    """Cost of what was filled versus the arrival midpoint, plus fees and missed trade.

    BUY: positive means paid above the benchmark. SELL: positive means sold below it.
    Fees and missed-trade cost are reported only when observed; otherwise they are
    listed as missing, never assumed to be zero.
    """
    with localcontext(ACCOUNTING_CONTEXT):
        sign = 1 if value.side == "BUY" else -1
        fill_rate = value.filled_shares / value.ordered_shares
        execution = Decimal(0)
        if value.filled_shares > 0:
            execution = sign * (value.fill_vwap - value.arrival_mid) * value.filled_shares
        observed, missing = ["execution_cost"], []
        fee = value.fees_paid
        (observed if fee is not None else missing).append("fee_cost")
        missed = None
        unfilled = value.ordered_shares - value.filled_shares
        if unfilled == 0:
            missed = Decimal(0)
            observed.append("missed_trade_cost")
        elif value.later_mid is not None:
            missed = sign * (value.later_mid - value.arrival_mid) * unfilled
            observed.append("missed_trade_cost")
        else:
            missing.append("missed_trade_cost")
        total = execution + (fee or Decimal(0)) + (missed or Decimal(0))
        base = value.arrival_mid * value.ordered_shares
        price_bps = (
            sign * (value.fill_vwap - value.arrival_mid) / value.arrival_mid * BPS
            if value.filled_shares > 0
            else None
        )
        return Shortfall(
            status="measured",
            benchmark_at=value.benchmark_at,
            arrival_mid=value.arrival_mid,
            fill_rate=fill_rate,
            execution_cost=execution,
            fee_cost=fee,
            missed_trade_cost=missed,
            total_cost=total,
            price_shortfall_bps=price_bps,
            total_shortfall_bps=total / base * BPS,
            components_observed=observed,
            components_missing=missing,
        )


class ShortfallGap(Contract):
    status: Literal["compared", "abstain"]
    reason: str | None = None
    estimated_price_shortfall_bps: Decimal | None = None
    actual_price_shortfall_bps: Decimal | None = None
    error_bps: Decimal | None = None
    estimated_fill_rate: Decimal | None = None
    actual_fill_rate: Decimal | None = None


def compare_estimate_to_actual(estimate: ExecutionEstimate, actual: Shortfall) -> ShortfallGap:
    """Estimated and actual shortfall are only comparable against the same benchmark."""
    if estimate.status != "modeled" or estimate.executable_vwap is None:
        return ShortfallGap(status="abstain", reason="ESTIMATE_HAS_NO_PRICE")
    if actual.status != "measured" or actual.price_shortfall_bps is None:
        return ShortfallGap(status="abstain", reason="ACTUAL_HAS_NO_FILL")
    if estimate.benchmark_at != actual.benchmark_at or estimate.arrival_mid != actual.arrival_mid:
        return ShortfallGap(status="abstain", reason="BENCHMARK_TIMESTAMP_OR_PRICE_MISMATCH")
    return ShortfallGap(
        status="compared",
        estimated_price_shortfall_bps=estimate.expected_cost_vs_mid_bps,
        actual_price_shortfall_bps=actual.price_shortfall_bps,
        error_bps=actual.price_shortfall_bps - estimate.expected_cost_vs_mid_bps,
        estimated_fill_rate=estimate.depth_coverage,
        actual_fill_rate=actual.fill_rate,
    )


# ── Matched-cohort comparison of two execution policies ─────────────────


class CohortOrder(Contract):
    decision_key: str = Field(min_length=1, max_length=120)
    total_shortfall_bps: Decimal
    fill_rate: Decimal = Field(ge=0, le=1)


class CohortComparison(Contract):
    status: Literal["compared", "insufficient_sample", "abstain"]
    reason: str | None = None
    matched_pairs: int = 0
    unmatched_baseline: int = 0
    unmatched_policy: int = 0
    min_pairs_required: int = 30
    mean_cost_delta_bps: Decimal | None = None
    p50_cost_delta_bps: Decimal | None = None
    p95_cost_delta_bps: Decimal | None = None
    mean_fill_rate_delta: Decimal | None = None
    profitability_claim: Literal[False] = False
    interpretation: str = (
        "Negative cost delta means the policy cost fewer bps than the baseline on the "
        "same decisions. It is a descriptive matched-pair result, not a profit claim."
    )


def _quantile(sorted_values: list[Decimal], q: Decimal) -> Decimal:
    """Linear interpolation between order statistics (deterministic)."""
    position = (len(sorted_values) - 1) * q
    lower = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def compare_matched_cohorts(
    baseline: list[CohortOrder], policy: list[CohortOrder], *, min_pairs: int = 30
) -> CohortComparison:
    """Compare two policies only on decisions present in both cohorts."""
    for cohort in (baseline, policy):
        if len({o.decision_key for o in cohort}) != len(cohort):
            return CohortComparison(status="abstain", reason="DUPLICATE_DECISION_KEY")
    base = {o.decision_key: o for o in baseline}
    pol = {o.decision_key: o for o in policy}
    keys = sorted(base.keys() & pol.keys())
    out = {
        "matched_pairs": len(keys),
        "unmatched_baseline": len(base) - len(keys),
        "unmatched_policy": len(pol) - len(keys),
        "min_pairs_required": min_pairs,
    }
    if not keys:
        return CohortComparison(status="abstain", reason="NO_MATCHED_DECISIONS", **out)
    with localcontext(ACCOUNTING_CONTEXT):
        cost = sorted(pol[k].total_shortfall_bps - base[k].total_shortfall_bps for k in keys)
        fill = [pol[k].fill_rate - base[k].fill_rate for k in keys]
        result = {
            "mean_cost_delta_bps": sum(cost, Decimal(0)) / len(cost),
            "p50_cost_delta_bps": _quantile(cost, Decimal("0.5")),
            "p95_cost_delta_bps": _quantile(cost, Decimal("0.95")),
            "mean_fill_rate_delta": sum(fill, Decimal(0)) / len(fill),
        }
    if len(keys) < min_pairs:
        return CohortComparison(
            status="insufficient_sample", reason="TOO_FEW_MATCHED_PAIRS", **out, **result
        )
    return CohortComparison(status="compared", **out, **result)
