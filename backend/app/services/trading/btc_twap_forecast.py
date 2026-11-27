"""Research-only probability of a future 60-second average clearing its strike."""

from math import isfinite, sqrt
from statistics import NormalDist, stdev


def settlement_probability(
    *,
    proxy_open: float,
    proxy_latest: float,
    closed_proxy_prices: list[float],
    seconds_remaining: float,
) -> float | None:
    """Use same-source moves as a proxy for movement from the settlement open.

    Assume zero drift and a constant short-term spot volatility. The average
    spans the last 60 seconds of the round and must lie entirely in the future.
    Proxy basis changes and jump risks are outside this approximation.
    """
    prices = [proxy_open, proxy_latest, *closed_proxy_prices]
    if (
        not isfinite(seconds_remaining)
        or seconds_remaining < 60
        or len(closed_proxy_prices) < 8
        or any(not isfinite(price) or price <= 0 for price in prices)
    ):
        return None
    changes = [
        later / earlier - 1
        for earlier, later in zip(closed_proxy_prices, closed_proxy_prices[1:], strict=False)
    ]
    per_second = stdev(changes) / sqrt(60)
    if per_second <= 0:
        return None
    # Future average of Brownian increments has variance sigma² * (T-2L/3).
    average_sigma = per_second * sqrt(seconds_remaining - 40)
    return NormalDist().cdf((proxy_latest / proxy_open - 1) / average_sigma)
