"""Synthetic traders and books for net-of-cost scoring tests (declared fixtures, not venue data)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.analytics.replay.follower import Book
from app.services.analytics.trader_scoring import ResolvedPosition

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def position(i, cost, pnl, day=None):
    return ResolvedPosition(
        market_id=f"m{i}",
        cost=Decimal(cost),
        pnl=Decimal(pnl),
        closed_at=T0 + timedelta(days=i if day is None else day),
    )


def favourite_buyer(markets=40):
    """Buys 95c favourites: wins 95% of the time, earns nothing net."""
    rows = []
    for i in range(markets):
        # cost 95, win pays +5; one loss in twenty costs -95 => net ~0
        rows.append(position(i, "95", "-95" if i % 20 == 19 else "5"))
    return rows


def steady_winner(markets=40, ret="0.08"):
    """Wins only 55% of markets but earns a solid net return per dollar."""
    rows = []
    for i in range(markets):
        win = i % 20 < 11
        rows.append(position(i, "100", "28" if win else "-24"))
    return rows


def book(snapshot, token, at, ask, bid=None, shares="1000", source_lag=0):
    ask_d = Decimal(ask)
    bid_d = Decimal(bid) if bid else ask_d - Decimal("0.01")
    return Book(
        snapshot_id=snapshot,
        token_id=token,
        observed_at=at,
        source_at=at - timedelta(seconds=source_lag),
        bids=[{"price": bid_d, "shares": Decimal(shares)}],
        asks=[{"price": ask_d, "shares": Decimal(shares)}],
    )
