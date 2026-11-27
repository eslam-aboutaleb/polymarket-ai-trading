"""Marketable-fill walk over a visible book with partial fills and honest labels."""

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.services.backtesting.contracts import BookLevel

ZERO = Decimal(0)


def round_price(price: Decimal, side: str, tick: Decimal) -> Decimal:
    """BUY limits round down and SELL limits round up: never more aggressive."""
    mode = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    return (price / tick).to_integral_value(rounding=mode) * tick


def round_shares(shares: Decimal, lot: Decimal) -> Decimal:
    return (shares / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


@dataclass
class FillWalk:
    fills: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    depth_quality: str = "displayed_depth"
    unfilled: Decimal = ZERO

    @property
    def shares(self) -> Decimal:
        return sum((s for _, s in self.fills), ZERO)


def walk(
    levels: list[BookLevel],
    side: str,
    quantity: Decimal,
    limit_price: Decimal,
    lot: Decimal,
    approximation_max_shares: Decimal,
    fee_bps: Decimal | None = None,
    budget: Decimal | None = None,
) -> FillWalk:
    """Take liquidity best price first, never beyond the limit or displayed size.

    ``levels`` are the opposite side of the book, best first. A level with no
    recorded size is not infinite: it is capped at ``approximation_max_shares``
    in total and the walk is labeled ``approximation_no_depth``.
    """
    result = FillWalk()
    remaining = quantity
    approx_left = approximation_max_shares
    spend_left = budget
    fee = Decimal(0) if fee_bps is None else fee_bps / 10000
    for level in levels:
        if remaining <= 0:
            break
        if (side == "BUY" and level.price > limit_price) or (
            side == "SELL" and level.price < limit_price
        ):
            break
        if level.shares is None:
            result.depth_quality = "approximation_no_depth"
            available = approx_left
        else:
            available = level.shares
        take = min(available, remaining)
        if spend_left is not None:
            take = min(take, spend_left / (level.price * (1 + fee)))
        take = round_shares(take, lot)
        if take <= 0:
            continue
        result.fills.append((level.price, take))
        remaining -= take
        if spend_left is not None:
            spend_left -= take * level.price * (1 + fee)
        if level.shares is None:
            approx_left -= take
    result.unfilled = remaining
    return result
