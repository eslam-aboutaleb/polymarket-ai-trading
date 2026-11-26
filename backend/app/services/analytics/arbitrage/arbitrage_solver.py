"""Worst-case net bound of a priced basket over a verified payoff matrix.

Quote-gross (before fees) and fee-inclusive bounds are reported separately.
Unknown cost items are listed and never silently treated as zero.
"""

from decimal import Decimal, localcontext

from pydantic import Field

from app.services.analytics.arbitrage.depth_aware_pricer import (
    PricedLeg,
    PricingResult,
    breakpoint_sizes,
    price_legs,
)
from app.services.analytics.arbitrage.payout_validator import PayoffMatrix
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import Contract

COST_ITEMS = ("gas_usdc", "settlement_usdc", "withdrawal_usdc", "bridge_fx_usdc")


class CostAssumptions(Contract):
    """Explicit non-trading costs in USDC. ``None`` means unknown, not zero."""

    gas_usdc: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    settlement_usdc: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    withdrawal_usdc: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    bridge_fx_usdc: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)

    def required(self, cross_venue: bool) -> tuple[str, ...]:
        return COST_ITEMS if cross_venue else ("gas_usdc", "settlement_usdc")

    def unknown(self, cross_venue: bool) -> list[str]:
        return [name for name in self.required(cross_venue) if getattr(self, name) is None]

    def known_total(self, cross_venue: bool) -> Decimal:
        return sum(
            (
                getattr(self, name)
                for name in self.required(cross_venue)
                if getattr(self, name) is not None
            ),
            Decimal(0),
        )


class NetBound(Contract):
    size: Decimal
    worst_case_payoff: Decimal
    best_case_payoff: Decimal
    gross_notional: Decimal
    total_fees: Decimal
    quote_gross_gap: Decimal
    fee_inclusive_bound: Decimal
    known_fixed_costs: Decimal
    unknown_costs: list[str]
    net_bound: Decimal
    exact: bool
    capital_required: Decimal
    return_on_capital_pct: Decimal | None


def solve_basket(
    pricing: PricingResult,
    matrix: PayoffMatrix,
    costs: CostAssumptions,
    *,
    cross_venue: bool,
) -> NetBound:
    """Bound for the priced size. Raises ValueError when not priced."""
    if pricing.status != "priced":
        raise ValueError("PRICING_NOT_AVAILABLE")
    with localcontext(ACCOUNTING_CONTEXT):
        size = pricing.common_size
        estimates = list(pricing.estimates.values())
        gross = sum((e.gross_notional or Decimal(0) for e in estimates), Decimal(0))
        fees = sum((e.fee or Decimal(0) for e in estimates), Decimal(0))
        cash = sum((e.net_cash or Decimal(0) for e in estimates), Decimal(0))
        worst = size * matrix.min_total
        best = size * matrix.max_total
        known = costs.known_total(cross_venue)
        unknown = costs.unknown(cross_venue)
        fee_inclusive = worst - cash
        net = fee_inclusive - known
        capital = cash + known
        roi = (net / capital * 100) if capital > 0 else None
        return NetBound(
            size=size,
            worst_case_payoff=worst,
            best_case_payoff=best,
            gross_notional=gross,
            total_fees=fees,
            quote_gross_gap=worst - gross,
            fee_inclusive_bound=fee_inclusive,
            known_fixed_costs=known,
            unknown_costs=unknown,
            net_bound=net,
            exact=not unknown,
            capital_required=capital,
            return_on_capital_pct=roi,
        )


def max_profitable_size(
    legs: list[PricedLeg],
    pricing: PricingResult,
    matrix: PayoffMatrix,
    costs: CostAssumptions,
    *,
    cross_venue: bool,
    max_age_seconds: Decimal,
    max_skew_seconds: Decimal,
) -> tuple[Decimal, Decimal] | None:
    """(size, net_bound) maximizing net bound over displayed-depth breakpoints.

    Returns None when no breakpoint size has a positive net bound.
    """
    best: tuple[Decimal, Decimal] | None = None
    for size in breakpoint_sizes(pricing, legs):
        priced = price_legs(
            legs, size, max_age_seconds=max_age_seconds, max_skew_seconds=max_skew_seconds
        )
        if priced.status != "priced":
            continue
        bound = solve_basket(priced, matrix, costs, cross_venue=cross_venue)
        if bound.net_bound > 0 and (best is None or bound.net_bound > best[1]):
            best = (bound.size, bound.net_bound)
    return best
