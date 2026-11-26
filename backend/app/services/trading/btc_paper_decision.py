"""Decision and scoring rules for the BTC five-minute paper benchmark."""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

FEE = Decimal("0.01")
MIN_EDGE = Decimal("0.02")
MAX_QUOTE_AGE_SECONDS = 20
MIN_SECONDS_TO_CLOSE = 20
MAX_PROXY_MOVE_BPS = Decimal("15")
MAX_PROXY_RANGE_BPS = Decimal("25")
MAX_MIDPOINT_MOVE = Decimal(".06")
MAX_SPREAD = Decimal(".04")
MAX_ASK_INCREASE = Decimal(".03")
MAX_LOSS_AT_RISK = Decimal("12")


def stress_reasons(
    *,
    proxy_move_bps: Decimal,
    proxy_range_bps: Decimal,
    forecast_midpoint: Decimal,
    execution_midpoint: Decimal,
    forecast_ask: Decimal | None,
    quote: "Quote | None",
) -> tuple[str, ...]:
    """Reject sudden movement symmetrically and price deterioration on the buy side."""
    checks = {
        "PROXY_SPIKE": abs(proxy_move_bps) >= MAX_PROXY_MOVE_BPS,
        "PROXY_RANGE_SPIKE": proxy_range_bps >= MAX_PROXY_RANGE_BPS,
        "MARKET_REPRICED": abs(execution_midpoint - forecast_midpoint) >= MAX_MIDPOINT_MOVE,
        "WIDE_SPREAD": quote is not None
        and (quote.bid is None or quote.ask - quote.bid > MAX_SPREAD),
        "ASK_REPRICED": quote is not None
        and (forecast_ask is None or quote.ask - forecast_ask >= MAX_ASK_INCREASE),
    }
    return tuple(reason for reason, blocked in checks.items() if blocked)


def eligible_arrival_quote(
    quote: "Quote | None",
    *,
    decision_at: float,
    delay_seconds: float,
    now: float,
) -> bool:
    """A pre-arrival book cannot prove liquidity after modeled execution delay."""
    return quote is not None and decision_at + delay_seconds <= quote.observed_at <= now


def spike_safe_limit(quote: "Quote", edge_limit: Decimal) -> Decimal:
    """Pin a price ceiling through the final simulator book refresh."""
    return min(edge_limit, quote.ask + quote.tick, Decimal(1))


def probability(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Expected a numeric probability")
    result = Decimal(str(value))
    if not result.is_finite() or not 0 <= result <= 1:
        raise ValueError("Probability out of range")
    return result


def blend_with_market(model: Decimal, midpoint: Decimal) -> Decimal:
    """Fixed comparison variant, frozen before observing any outcomes."""
    return (model + midpoint) / 2


def brier_score(p_up: Decimal, up: bool) -> Decimal:
    return (p_up - int(up)) ** 2


def proxy_baselines(change: Decimal) -> dict[str, Decimal]:
    """Fixed hypotheses evaluated against the same outcomes as the model."""
    if not change.is_finite():
        raise ValueError("Proxy return must be finite")
    momentum = Decimal(".60") if change > 0 else Decimal(".40") if change < 0 else Decimal(".50")
    return {"momentum": momentum, "mean_reversion": 1 - momentum}


def visible_buy_estimate(
    asks: list[tuple[Decimal, Decimal]],
    limit: Decimal,
    notional: Decimal,
    *,
    lot: Decimal = Decimal(".01"),
) -> dict:
    """Walk captured asks for diagnostics. Only the native simulator records fills."""
    remaining = notional
    shares = Decimal(0)
    fills = []
    for price, depth in sorted(asks):
        if price > limit:
            break
        quantity = min(depth, remaining / price)
        quantity = (quantity / lot).to_integral_value(rounding=ROUND_FLOOR) * lot
        if quantity > 0:
            fills.append((price, quantity))
        shares += quantity
        remaining -= quantity * price
    spent = notional - remaining
    return {
        "shares": shares,
        "notional": spent,
        "unfilled_notional": remaining,
        "vwap": spent / shares if shares else None,
        "modeled_fee": spent * FEE,
        "levels": fills,
    }


@dataclass(frozen=True)
class Quote:
    ask: Decimal
    tick: Decimal
    observed_at: float
    visible_shares: Decimal
    minimum_shares: Decimal
    bid: Decimal | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    edges: dict[str, Decimal]
    limit: Decimal | None = None


def decide(
    p_up: Decimal,
    quotes: dict[str, Quote],
    *,
    now: float,
    close_at: int,
    reference_verified: bool,
    budget: Decimal = Decimal("5"),
    market_midpoint: Decimal | None = None,
    venue_taker_rate: Decimal = Decimal(0),
) -> Decision:
    """Evaluate both sides even when reference quality forces abstention."""
    p_up = probability(p_up)
    probabilities = {"Up": p_up, "Down": 1 - p_up}
    # For buys at or above the preview ask this bounds both fee models.
    rates = {side: max(FEE, venue_taker_rate * (1 - quote.ask)) for side, quote in quotes.items()}
    edges = {
        side: probabilities[side] - quotes[side].ask * (1 + rates[side])
        for side in probabilities
        if side in quotes
    }
    if not reference_verified:
        return Decision("HOLD", "MISSING_CHAINLINK_OPEN", edges)
    if close_at - now <= MIN_SECONDS_TO_CLOSE:
        return Decision("HOLD", "TOO_CLOSE_TO_EXPIRY", edges)
    for side in sorted(edges, key=edges.__getitem__, reverse=True):
        quote = quotes[side]
        if (
            edges[side] < MIN_EDGE
            or not 0 < quote.ask <= 1
            or quote.tick <= 0
            or not 0 <= now - quote.observed_at <= MAX_QUOTE_AGE_SECONDS
            or min(quote.visible_shares, budget / (quote.ask * (1 + rates[side])))
            < quote.minimum_shares
        ):
            continue
        ceiling = (probabilities[side] - MIN_EDGE) / (1 + rates[side])
        limit = (ceiling / quote.tick).to_integral_value(rounding=ROUND_FLOOR) * quote.tick
        if limit >= quote.ask:
            # Until the model has demonstrated held-out skill, refrain from
            # taking the opposite side of the market on model disagreement alone.
            if market_midpoint is not None:
                market_up = probability(market_midpoint)
                market_probability = market_up if side == "Up" else 1 - market_up
                if market_probability <= Decimal(".5"):
                    return Decision("HOLD", "COUNTER_MARKET_EDGE_UNVERIFIED", edges)
            return Decision(side, "AFTER_FEE_EDGE", edges, limit)
    return Decision("HOLD", "NO_EXECUTABLE_EDGE", edges)
