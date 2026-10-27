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


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D(-1), 1.0])
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


def test_ambient_precision_and_rounding_do_not_change_economic_booking():
    from decimal import ROUND_UP, localcontext

    reference = PaperLedger(D(".9999999"))
    reference.reserve("o", "YES", "BUY", D(3), D(".3333333"), D(0))
    reference.fill("f", "o", D(3), D(".3333333"))
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_UP
        actual = PaperLedger(D(".9999999"))
        actual.reserve("o", "YES", "BUY", D(3), D(".3333333"), D(0))
        actual.fill("f", "o", D(3), D(".3333333"))
        assert actual.value({"YES": D(".5")}) == reference.value({"YES": D(".5")})
    assert actual.journal == reference.journal
    assert actual.cash == 0


def test_fractional_full_fill_does_not_overdraw():
    ledger = PaperLedger(D("455058225.4469318636965307083"))
    with pytest.raises(ValueError, match="insufficient cash"):
        ledger.reserve("o", "YES", "BUY", D("444564525.4895505"), D(".96235238"), D("636.4827341"))
    assert not ledger.journal and not ledger.orders
    ledger = PaperLedger(D("455058226"))
    ledger.reserve("o", "YES", "BUY", D("444564525.4895505"), D(".96235238"), D("636.4827341"))
    ledger.fill("f", "o", D("444564525.4895505"), D(".96235238"))
    assert ledger.cash >= 0 and not ledger.orders


def test_failed_invariant_cannot_commit_state_or_consume_event_id(monkeypatch):
    from copy import deepcopy

    from app.services.analytics.replay.ledger import PaperLedger as Ledger

    ledger = PaperLedger(D(100))
    ledger.reserve("o", "YES", "BUY", D(10), D(".5"), D(0))
    before = deepcopy(ledger.__dict__)
    original = Ledger._record

    def broken_record(self, event_id, payload):
        original(self, event_id, payload)
        raise AssertionError("injected invariant failure")

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "_record", broken_record)
        with pytest.raises(AssertionError):
            ledger.fill("f", "o", D(10), D(".5"))
    assert ledger.__dict__ == before
    ledger.fill("f", "o", D(10), D(".5"))
    assert ledger.cash == 95


def test_zero_funding_is_valid_but_cannot_reserve_unfunded_cash():
    ledger = PaperLedger(D(0))
    assert D(ledger.value({})["nav"]) == 0
    with pytest.raises(ValueError, match="cash"):
        ledger.reserve("unfunded", "YES", "BUY", D(1), D(".5"), D(0))
