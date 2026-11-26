from datetime import timedelta
from decimal import Decimal

import pytest
from trader_eval_fixtures import T0, book

from app.services.analytics.follower_lag import (
    LagSettings,
    LeaderTrade,
    copy_skip_decision,
    estimated_edge,
    lag_report,
    simulate_follower_fill,
)


def trade(trade_id="t1", price="0.40", payout="1", side="BUY", at=None):
    return LeaderTrade(
        trade_id=trade_id,
        token_id="YES",
        side=side,
        leader_price=Decimal(price),
        first_seen_at=at or T0,
        payout=None if payout is None else Decimal(payout),
    )


def books():
    return [
        book("b0", "YES", T0, "0.40"),
        book("b60", "YES", T0 + timedelta(seconds=60), "0.46"),
        book("b300", "YES", T0 + timedelta(seconds=300), "0.55"),
    ]


def test_delay_raises_follower_price_and_cuts_return():
    report = lag_report([trade()], books(), [0, 60, 300], LagSettings(order_notional=Decimal(40)))
    by_delay = {s["delay_seconds"]: s for s in report["scenarios"]}
    slip = [Decimal(by_delay[d]["median_slippage"]) for d in (0, 60, 300)]
    assert slip == [Decimal("0"), Decimal("0.06"), Decimal("0.15")]
    returns = [Decimal(by_delay[d]["median_net_follower_return"]) for d in (0, 60, 300)]
    assert returns[0] > returns[1] > returns[2]
    assert returns[0] == Decimal("1.5")


def test_follower_return_never_uses_leader_pnl():
    # The leader paid 0.40 and the token pays 1; a follower filling at 0.55 earns less.
    row = simulate_follower_fill(trade(), books(), 300, LagSettings())
    leader_return = Decimal("1") / Decimal("0.40") - 1
    assert Decimal(row["follower_return"]) < leader_return
    assert Decimal(row["vwap"]) == Decimal("0.55")


def test_fees_reduce_net_return():
    free = simulate_follower_fill(trade(), books(), 0, LagSettings(fee_bps=Decimal(0)))
    fee = simulate_follower_fill(trade(), books(), 0, LagSettings(fee_bps=Decimal(200)))
    assert Decimal(fee["follower_return"]) < Decimal(free["follower_return"])


def test_missing_book_stale_book_and_no_depth_stay_unfilled():
    assert (
        simulate_follower_fill(trade(), [], 0, LagSettings())["reason"] == "missing_eligible_book"
    )
    stale = [book("s", "YES", T0, "0.4", source_lag=3600)]
    assert simulate_follower_fill(trade(), stale, 0, LagSettings())["reason"] == "stale_book"
    empty = book("e", "YES", T0, "0.4")
    empty = empty.model_copy(update={"asks": []})
    assert simulate_follower_fill(trade(), [empty], 0, LagSettings())["reason"] == "missing_depth"


def test_impossible_size_is_partial_not_full():
    thin = [book("b0", "YES", T0, "0.40", shares="5")]
    row = simulate_follower_fill(trade(), thin, 0, LagSettings(order_notional=Decimal(100)))
    assert row["status"] == "partial"
    assert Decimal(row["cost"]) == Decimal("2.00")


def test_unsettled_trades_get_no_return_and_duplicates_count_once():
    report = lag_report([trade(payout=None), trade(payout=None)], books(), [0], LagSettings())
    scenario = report["scenarios"][0]
    assert scenario["signals"] == 1
    assert scenario["settled_trades"] == 0
    assert scenario["median_net_follower_return"] is None
    assert scenario["fill_rate"] == "1"


def test_sell_slippage_is_leader_minus_follower():
    sell_books = [book("b", "YES", T0, "0.62", bid="0.60")]
    row = simulate_follower_fill(trade(side="SELL", price="0.65"), sell_books, 0, LagSettings())
    assert Decimal(row["slippage"]) == Decimal("0.05")
    assert "follower_return" not in row


def test_skip_rule_boundaries_and_configurable_fraction():
    edge = estimated_edge(0.10, Decimal("0.50"))
    assert edge == Decimal("0.05")
    common = {"side": "BUY", "leader_price": Decimal("0.50"), "edge": edge}
    ok = copy_skip_decision(
        current_price=Decimal("0.52"), max_edge_fraction=Decimal("0.5"), **common
    )
    assert ok["skip"] is False
    skip = copy_skip_decision(
        current_price=Decimal("0.53"), max_edge_fraction=Decimal("0.5"), **common
    )
    assert skip["skip"] is True and skip["reason"] == "price_moved_beyond_edge_fraction"
    loose = copy_skip_decision(
        current_price=Decimal("0.53"), max_edge_fraction=Decimal("1"), **common
    )
    assert loose["skip"] is False
    favourable = copy_skip_decision(
        current_price=Decimal("0.40"), max_edge_fraction=Decimal("0"), **common
    )
    assert favourable["skip"] is False
    sell = copy_skip_decision(
        side="SELL",
        leader_price=Decimal("0.50"),
        current_price=Decimal("0.47"),
        edge=edge,
        max_edge_fraction=Decimal("0.5"),
    )
    assert sell["skip"] is True


def test_skip_when_no_edge_and_input_validation():
    assert estimated_edge(None, Decimal("0.5")) == 0
    assert estimated_edge(-0.1, Decimal("0.5")) == 0
    decision = copy_skip_decision(
        side="BUY",
        leader_price=Decimal("0.5"),
        current_price=Decimal("0.5"),
        edge=Decimal(0),
        max_edge_fraction=Decimal(1),
    )
    assert decision["skip"] and decision["reason"] == "no_estimated_edge"
    with pytest.raises(ValueError):
        copy_skip_decision(
            side="HOLD",
            leader_price=Decimal("0.5"),
            current_price=Decimal("0.5"),
            edge=Decimal(1),
            max_edge_fraction=Decimal(1),
        )
    with pytest.raises(ValueError):
        copy_skip_decision(
            side="BUY",
            leader_price=Decimal("0.5"),
            current_price=Decimal("0.5"),
            edge=Decimal(1),
            max_edge_fraction=Decimal(-1),
        )
