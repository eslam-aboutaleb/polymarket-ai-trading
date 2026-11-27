from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.trading.copy_quote import copy_limit, quote_plan

NOW = datetime(2026, 11, 23, tzinfo=UTC)
RULES = {"tick_size": ".01", "min_order_size": "5"}


def book(*, ask=".51", bid=".49", age=0, size="100"):
    return SimpleNamespace(
        source_at=NOW - timedelta(seconds=age),
        observed_at=NOW,
        asks=[SimpleNamespace(price=Decimal(ask), shares=Decimal(size))],
        bids=[SimpleNamespace(price=Decimal(bid), shares=Decimal(size))],
    )


def test_limits_round_towards_the_slippage_bound():
    assert copy_limit("BUY", ".5", ".05", Decimal(".01")) == Decimal(".52")
    assert copy_limit("SELL", ".5", ".05", Decimal(".01")) == Decimal(".48")


def test_buy_uses_full_fok_depth_and_stays_within_budget():
    limit, shares, expires = quote_plan(book(), RULES, "BUY", ".5", ".05", "10", NOW)
    assert shares == Decimal("19.23")
    assert limit * shares <= 10
    assert expires == NOW + timedelta(seconds=5)


@pytest.mark.parametrize(
    "snapshot,side,reason",
    [
        (book(age=5), "BUY", "stale"),
        (book(age=-1), "SELL", "future"),
        (book(ask=".54"), "BUY", "depth"),
        (book(bid=".47"), "SELL", "depth"),
        (book(size="10"), "BUY", "depth"),
    ],
)
def test_stale_moved_or_shallow_books_cannot_reach_submission(snapshot, side, reason):
    with pytest.raises(ValueError, match=reason):
        quote_plan(snapshot, RULES, side, ".5", ".05", "10", NOW)
