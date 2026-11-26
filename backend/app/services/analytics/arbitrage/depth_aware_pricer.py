"""Walk the real ask book of every leg at one common size.

Midpoints and ``outcomePrices`` are never fills. Each leg is priced with the
verified ``binary_book`` walker (#18); the basket size is the largest size
every leg can fill at, aligned to the coarsest lot, or the request is rejected.
"""

from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Literal

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import (
    BinaryBook,
    Contract,
    CostEstimate,
    MarketSpec,
    WalkRequest,
    walk_depth,
)

DEFAULT_MAX_AGE_SECONDS = Decimal(60)
DEFAULT_MAX_SKEW_SECONDS = Decimal(5)
MAX_BREAKPOINTS = 200


class PricedLeg(Contract):
    label: str
    spec: MarketSpec
    book: BinaryBook
    outcome: Literal["YES", "NO"]
    fee_bps: Decimal | None = None
    fee_version: str | None = None

    @property
    def token_id(self) -> str:
        return self.spec.yes_token if self.outcome == "YES" else self.spec.no_token


class PricingResult(Contract):
    status: Literal["priced", "abstain"]
    reasons: list[str]
    stale: bool = False
    requested_size: Decimal
    common_size: Decimal = Decimal(0)
    partial: bool = False
    max_executable_size: Decimal = Decimal(0)
    estimates: dict[str, CostEstimate] = {}


def _lot_floor(size: Decimal, lot: Decimal) -> Decimal:
    return (size / lot).to_integral_value(rounding=ROUND_DOWN) * lot


def _walk(leg: PricedLeg, size: Decimal) -> CostEstimate:
    return walk_depth(
        leg.spec,
        leg.book,
        WalkRequest(
            side="BUY",
            units="SHARES",
            amount=size,
            fee_bps=leg.fee_bps,
            fee_version=leg.fee_version,
        ),
    )


def walk_all(legs: list[PricedLeg], size: Decimal) -> dict[str, CostEstimate]:
    return {leg.label: _walk(leg, size) for leg in legs}


def price_legs(
    legs: list[PricedLeg],
    requested_size: Decimal,
    *,
    max_age_seconds: Decimal = DEFAULT_MAX_AGE_SECONDS,
    max_skew_seconds: Decimal = DEFAULT_MAX_SKEW_SECONDS,
) -> PricingResult:
    """Price every leg at ``requested_size`` or the largest common fillable size."""

    def abstain(reasons: list[str], *, stale: bool = False, estimates=None) -> PricingResult:
        return PricingResult(
            status="abstain",
            reasons=sorted(set(reasons)),
            stale=stale,
            requested_size=requested_size,
            estimates=estimates or {},
        )

    with localcontext(ACCOUNTING_CONTEXT):
        reasons: list[str] = []
        stale = False
        for leg in legs:
            if leg.book.token_id != leg.token_id:
                reasons.append(f"TOKEN_MISMATCH:{leg.label}")
            if leg.book.grade == "STALE":
                stale = True
                reasons.append(f"STALE_BOOK:{leg.label}")
            elif leg.book.grade != "VERIFIED_BOOK":
                reasons.append(f"BOOK_NOT_USABLE:{leg.label}")
            elif leg.book.age_seconds is None:
                reasons.append(f"BOOK_AGE_UNAVAILABLE:{leg.label}")
            elif leg.book.age_seconds > max_age_seconds:
                stale = True
                reasons.append(f"STALE_BOOK:{leg.label}")
        if reasons:
            return abstain(reasons, stale=stale)
        ages = [leg.book.age_seconds for leg in legs]
        if len(legs) > 1 and max(ages) - min(ages) > max_skew_seconds:
            return abstain(["ASYNCHRONOUS_BOOKS"], stale=True)

        first = walk_all(legs, requested_size)
        fee_gaps = [
            f"UNKNOWN_FEES:{label}"
            for label, est in first.items()
            if est.reason == "FEE_POLICY_UNKNOWN"
        ]
        if fee_gaps:
            return abstain(fee_gaps, estimates=first)
        failed = [
            f"{est.reason}:{label}" for label, est in first.items() if est.status != "modeled"
        ]
        if failed:
            return abstain(failed, estimates=first)

        coarsest_lot = max(leg.spec.lot for leg in legs)
        common = _lot_floor(min(est.matched_shares for est in first.values()), coarsest_lot)
        floor = max(leg.spec.minimum_shares for leg in legs)
        if common < floor:
            return abstain(["INSUFFICIENT_COMMON_DEPTH"], estimates=first)
        final = (
            first
            if common == min(est.matched_shares for est in first.values())
            and all(est.matched_shares == common for est in first.values())
            else walk_all(legs, common)
        )
        failed = [
            f"{est.reason}:{label}" for label, est in final.items() if est.status != "modeled"
        ]
        if failed:
            return abstain(failed, estimates=final)
        capacity = min(est.displayed_capacity or Decimal(0) for est in final.values())
        return PricingResult(
            status="priced",
            reasons=[],
            requested_size=requested_size,
            common_size=common,
            partial=common < requested_size,
            max_executable_size=_lot_floor(capacity, coarsest_lot),
            estimates=final,
        )


def breakpoint_sizes(result: PricingResult, legs: list[PricedLeg]) -> list[Decimal]:
    """Candidate basket sizes at displayed-depth boundaries (<= common size)."""
    sizes: set[Decimal] = {result.common_size}
    lot = max(leg.spec.lot for leg in legs)
    for est in result.estimates.values():
        for point in est.curve:
            size = _lot_floor(point.cumulative_shares, lot)
            if 0 < size <= result.common_size:
                sizes.add(size)
    return sorted(sizes)[:MAX_BREAKPOINTS]
