"""Venue evidence for the BTC paper benchmark."""

from decimal import Decimal


def taker_coefficient(clob_market: dict, outcomes: dict[str, str]) -> Decimal:
    """Validate the market's token identities and supported taker curve."""
    tokens = {token["o"]: str(token["t"]) for token in clob_market["t"]}
    curve = clob_market["fd"]
    if tokens != outcomes or len(clob_market["t"]) != 2:
        raise ValueError("Fee schedule outcome identity mismatch")
    if curve["e"] != 1 or curve["to"] is not True or isinstance(curve["r"], bool):
        raise ValueError("Unsupported fee curve")
    rate = Decimal(str(curve["r"]))
    if not rate.is_finite() or not 0 <= rate <= 1:
        raise ValueError("Invalid fee coefficient")
    return rate


def taker_fee(price: Decimal, shares: Decimal, rate: Decimal) -> Decimal:
    return shares * rate * price * (1 - price)


def reported_reference(payload: dict, start: int, observed_at: float) -> dict:
    """Validate the venue's window-specific publication, not raw stream ticks."""
    source_at = payload["timestamp"]
    if type(source_at) is not int or not start * 1000 <= source_at <= observed_at * 1000:
        raise ValueError("Reference timestamp outside observed window")
    prices = {}
    for name in ("openPrice", "closePrice"):
        value = payload.get(name)
        if value is None and name == "closePrice":
            continue
        price = Decimal(str(value))
        if not price.is_finite() or price <= 0:
            raise ValueError("Invalid published reference price")
        prices[name] = str(price)
    return {**prices, "timestamp": source_at, "completed": payload.get("completed") is True}


def resolved_reference_side(observation: dict) -> str | None:
    if not observation["completed"] or "closePrice" not in observation:
        return None
    return (
        "Up" if Decimal(observation["closePrice"]) >= Decimal(observation["openPrice"]) else "Down"
    )
