"""One-leg failure exposure. Legs are separate orders; there is no atomicity.

If the first leg fills and the second does not, the account holds a single
outcome token. The loss is bounded by what that leg cost; a modeled unwind
(sell back into displayed bids) may recover part of it.
"""

from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Literal

from app.services.analytics.arbitrage.depth_aware_pricer import PricedLeg, PricingResult
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import Contract, WalkRequest, walk_depth

FILL_STRESS = (Decimal(1), Decimal("0.5"))


class OneLegCase(Contract):
    filled_leg: str
    failed_leg: str
    fill_fraction: Decimal
    cash_spent: Decimal
    hold_to_resolution_max_loss: Decimal
    unwind_status: Literal["modeled", "unavailable"]
    unwind_proceeds: Decimal | None
    modeled_unwind_loss: Decimal | None
    bound: Decimal


class OneLegExposure(Contract):
    cases: list[OneLegCase]
    max_loss_bound: Decimal
    non_atomic: bool = True
    notes: list[str] = [
        "legs_execute_as_separate_orders",
        "bound_is_loss_of_cash_spent_on_the_filled_leg",
        "unwind_uses_displayed_bids_only_no_fill_probability",
    ]


def one_leg_exposure(legs: list[PricedLeg], pricing: PricingResult) -> OneLegExposure:
    if pricing.status != "priced":
        raise ValueError("PRICING_NOT_AVAILABLE")
    cases: list[OneLegCase] = []
    with localcontext(ACCOUNTING_CONTEXT):
        for filled in legs:
            others = [leg.label for leg in legs if leg.label != filled.label]
            estimate = pricing.estimates[filled.label]
            for fraction in FILL_STRESS:
                lot = filled.spec.lot
                shares = (estimate.matched_shares * fraction / lot).to_integral_value(
                    rounding=ROUND_DOWN
                ) * lot
                if shares < filled.spec.minimum_shares:
                    continue
                buy = walk_depth(
                    filled.spec,
                    filled.book,
                    WalkRequest(
                        side="BUY",
                        amount=shares,
                        fee_bps=filled.fee_bps,
                        fee_version=filled.fee_version,
                    ),
                )
                if buy.status != "modeled":
                    continue
                spent = buy.net_cash
                sell = walk_depth(
                    filled.spec,
                    filled.book,
                    WalkRequest(
                        side="SELL",
                        amount=buy.matched_shares,
                        fee_bps=filled.fee_bps,
                        fee_version=filled.fee_version,
                    ),
                )
                modeled = sell.status == "modeled"
                proceeds = sell.net_cash if modeled else None
                # Selling back may be partial; unsold shares are held to
                # resolution, so the bound stays the full cash spent.
                unwind_loss = spent - proceeds if modeled else None
                bound = spent
                for other in others:
                    cases.append(
                        OneLegCase(
                            filled_leg=filled.label,
                            failed_leg=other,
                            fill_fraction=fraction,
                            cash_spent=spent,
                            hold_to_resolution_max_loss=spent,
                            unwind_status="modeled" if modeled else "unavailable",
                            unwind_proceeds=proceeds,
                            modeled_unwind_loss=unwind_loss,
                            bound=bound,
                        )
                    )
        worst = max((case.bound for case in cases), default=Decimal(0))
    return OneLegExposure(cases=cases, max_loss_bound=worst)
