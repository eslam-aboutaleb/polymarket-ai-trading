"""Golden economic fixtures for the shared accounting contract (#27/#35/#55)."""

from decimal import Decimal as D

import pytest

from app.services.analytics.replay.ledger import PaperLedger


def buy(ledger, shares="25", price="0.40", fee="0", order="buy"):
    ledger.reserve(order, "YES", "BUY", D(shares), D(price), D(fee))
    ledger.fill(order + "-fill", order, D(shares), D(price))


def test_nav_mark_move_partial_close_and_settlement_reconcile():
    ledger = PaperLedger(D("1000"))
    buy(ledger)
    value = ledger.value({"YES": D("0.60")})
    assert D(value["cash"]) == 990
    assert D(value["nav"]) == 1005
    assert D(value["unrealized_pnl"]) == 5
    ledger.reserve("sell", "YES", "SELL", D(10), D("0.70"), D(0))
    ledger.fill("sell-fill", "sell", D(10), D("0.70"))
    value = ledger.value({"YES": D("0.60")})
    assert D(value["cash"]) == 997
    assert D(value["realized_pnl"]) == 3
    assert D(value["unrealized_pnl"]) == 3
    assert D(value["net_pnl"]) == 6
    ledger.settle("resolution", "YES", D(1))
    value = ledger.value({})
    assert D(value["nav"]) == 1012
    assert D(value["realized_pnl"]) == D(value["net_pnl"]) == 12


def test_reservations_partial_fill_cancel_and_retry_book_once():
    ledger = PaperLedger(D(100))
    ledger.reserve("o", "YES", "BUY", D(100), D("0.5"), D(100))
    assert ledger.reserved_cash == D("50.5")
    assert ledger.value({})["nav"] == "100"
    for _ in range(2):
        ledger.fill("f", "o", D(20), D("0.4"))
    assert ledger.cash == D("91.92")
    assert ledger.positions["YES"].shares == 20
    assert ledger.reserved_cash == D("40.4")
    for _ in range(2):
        ledger.cancel("c", "o")
    assert ledger.reserved_cash == 0
    assert len(ledger.journal) == 3
    with pytest.raises(ValueError, match="not open"):
        ledger.fill("late", "o", D(1), D("0.4"))
    with pytest.raises(ValueError, match="conflicts"):
        ledger.fill("f", "o", D(21), D("0.4"))


@pytest.mark.parametrize("payout", ["0", "0.5", "1"])
def test_resolution_void_and_fee_conservation(payout):
    ledger = PaperLedger(D(100))
    buy(ledger, fee="100")
    ledger.settle("s", "YES", D(payout))
    ledger.settle("s", "YES", D(payout))
    assert ledger.cash == D(100) - D("10.1") + D(25) * D(payout)
    assert ledger.fees == D("0.1")
    assert D(ledger.value({})["net_pnl"]) == ledger.realized_pnl
    with pytest.raises(ValueError):
        ledger.settle("s2", "YES", D(1))


def test_missing_mark_unknown_even_though_cash_and_realized_are_known():
    ledger = PaperLedger(D(1000))
    buy(ledger)
    value = ledger.value({})
    assert value["nav"] is None and value["net_pnl"] is None
    assert value["missing_marks"] == ["YES"]
    assert D(ledger.value({"YES": D(0)})["nav"]) == 990


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D(-1), D(0), 1.0])
def test_invalid_funding_rejected(bad):
    with pytest.raises(ValueError):
        PaperLedger(bad)


@pytest.mark.parametrize(
    "side,shares,price,fee",
    [
        ("BUY", "0", ".5", "0"),
        ("BOGUS", "1", ".5", "0"),
        ("BUY", "1", "1.1", "0"),
        ("BUY", "NaN", ".5", "0"),
        ("BUY", "1", ".5", "-1"),
        ("BUY", "1", ".5", "10001"),
        ("SELL", "1", ".5", "0"),
        ("BUY", "1000", ".5", "0"),
    ],
)
def test_failed_reservation_does_not_mutate(side, shares, price, fee):
    ledger = PaperLedger(D(100))
    with pytest.raises(ValueError):
        ledger.reserve("o", "YES", side, D(shares), D(price), D(fee))
    assert ledger.cash == 100 and not ledger.orders and not ledger.journal


def test_reservation_cannot_spend_reserved_cash_or_sell_reserved_shares():
    ledger = PaperLedger(D(100))
    ledger.reserve("o", "YES", "BUY", D(150), D(".5"), D(0))
    with pytest.raises(ValueError, match="cash"):
        ledger.reserve("o2", "YES", "BUY", D(100), D(".5"), D(0))
    ledger.fill("f", "o", D(150), D(".5"))
    ledger.reserve("s", "YES", "SELL", D(100), D(".5"), D(0))
    with pytest.raises(ValueError, match="inventory"):
        ledger.reserve("s2", "YES", "SELL", D(100), D(".5"), D(0))
    with pytest.raises(ValueError, match="open orders"):
        ledger.settle("terminal", "YES", D(1))


@pytest.mark.parametrize("qty,price", [("30", ".4"), ("1", ".6"), ("-1", ".4")])
def test_invalid_fill_is_atomic(qty, price):
    ledger = PaperLedger(D(100))
    ledger.reserve("o", "YES", "BUY", D(25), D(".5"), D(0))
    with pytest.raises(ValueError):
        ledger.fill("f", "o", D(qty), D(price))
    assert ledger.cash == 100 and not ledger.positions and len(ledger.journal) == 1


def test_independent_ledgers_and_replay_are_identical():
    ledgers = [PaperLedger(D(1000)), PaperLedger(D(1000))]
    for ledger in ledgers:
        buy(ledger, fee="25")
    assert ledgers[0].journal == ledgers[1].journal
    ledgers[0].settle("s", "YES", D(1))
    assert ledgers[1].positions["YES"].shares == 25
