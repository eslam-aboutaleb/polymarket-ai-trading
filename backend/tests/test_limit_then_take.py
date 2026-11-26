"""Limit-then-take executor, shortfall metrics, paper comparison, stop-loss interval (#157).

The venue below is a deterministic simulator that honors idempotency keys the way the real
gateway does; it is the system under test's counterparty, not a mock of the code under test.
"""

from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_live_gateway import gateway  # noqa: F401  (real gateway fixture)

from app.models import Base
from app.models.trading.execution_shortfall import ExecutionShortfall
from app.models.users.user import User
from app.services.trading.execution.limit_then_take import (
    Book,
    Decision,
    GatewayVenue,
    OrderState,
    Params,
    VenueTimeout,
    order_key,
    run_limit_then_take,
)
from app.services.trading.execution.shortfall import (
    ComparisonCase,
    compare_modes,
    record_shortfall,
    summarize,
)
from app.services.trading.stop_loss_monitor import interval_for_titles


class SimVenue:
    def __init__(
        self,
        book,
        maker_fill=D(0),
        take_fill=None,
        lose_first_place=False,
        timeouts=0,
        cancel_works=True,
    ):
        self._book, self.maker_fill, self.take_fill = book, maker_fill, take_fill
        self.lose_first_place, self.timeouts, self.cancel_works = (
            lose_first_place,
            timeouts,
            cancel_works,
        )
        self.orders: dict[str, dict] = {}
        self.place_calls: list[str] = []
        self.t = 0.0

    def book(self, token_id):
        return self._book

    def place(self, *, key, token_id, side, price, shares, order_type, post_only):
        self.place_calls.append(key)
        if key not in self.orders:  # idempotent: replay returns the existing order
            fill = (
                self.maker_fill
                if order_type == "GTC"
                else (shares if self.take_fill is None else self.take_fill)
            )
            state = "filled" if fill >= shares else ("partial" if fill else "open")
            if order_type == "FOK":
                state = "filled" if fill >= shares else "cancelled"
                fill = fill if fill >= shares else D(0)
            self.orders[key] = {"state": state, "fill": min(fill, shares), "price": price}
            if self.lose_first_place and len(self.place_calls) == 1:
                raise VenueTimeout  # applied, but the response was lost
        if self.timeouts > 0:
            self.timeouts -= 1
            raise VenueTimeout
        return self._state(key)

    def _state(self, key):
        o = self.orders[key]
        return OrderState(o["state"], o["fill"], o["price"] if o["fill"] else None)

    def status(self, key):
        if self.timeouts > 0:
            self.timeouts -= 1
            raise VenueTimeout
        return self._state(key)

    def cancel(self, key):
        if self.timeouts > 0:
            self.timeouts -= 1
            raise VenueTimeout
        o = self.orders[key]
        if self.cancel_works and o["state"] in ("open", "partial"):
            o["state"] = "cancelled"
        return self._state(key)


def clock_sleep():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


BOOK = Book(D("0.49"), D("0.51"))
DEC = Decision("d1", "copy", "tok", "BUY", D(10), D("0.50"), D("0.60"))
PARAMS = Params(
    timeout_seconds=5, poll_seconds=1, taker_fee_rate=D("0.05"), max_take_slippage=D("0.05")
)


def run(venue, decision=DEC, params=PARAMS):
    clock, sleep = clock_sleep()
    return run_limit_then_take(decision, params, venue, clock=clock, sleep=sleep)


def test_maker_fills_fully_no_taker_leg():
    v = SimVenue(BOOK, maker_fill=D(10))
    r = run(v)
    assert r.outcome == "FILLED_MAKER" and r.filled == 10 and r.fees == 0
    assert len(v.place_calls) == 1


def test_unfilled_maker_then_taker_after_cancel():
    v = SimVenue(BOOK)
    r = run(v)
    assert r.outcome == "FILLED_TAKER" and r.taker_filled == 10 and r.fees > 0
    assert v.orders[r.keys[0]]["state"] == "cancelled"
    assert v.place_calls == r.keys  # maker leg first, then the take leg


def test_partial_maker_then_take_remainder_no_double_fill():
    v = SimVenue(BOOK, maker_fill=D(4))
    r = run(v)
    assert r.outcome == "FILLED_MIXED" and r.maker_filled == 4 and r.taker_filled == 6
    assert r.filled == 10


def test_lost_place_response_retries_same_key_and_never_double_places():
    v = SimVenue(BOOK, maker_fill=D(10), lose_first_place=True)
    r = run(v)
    assert r.outcome == "FILLED_MAKER"
    assert len(v.orders) == 1 and len(set(v.place_calls)) == 1 and len(v.place_calls) == 2


def test_taker_never_sent_when_maker_cancel_unconfirmed():
    v = SimVenue(BOOK, cancel_works=False)
    r = run(v)
    assert r.outcome == "UNRESOLVED_ORDER_STATE"
    assert all(k == r.keys[0] for k in v.place_calls)


def test_repeated_timeouts_become_unknown_not_a_fill():
    v = SimVenue(BOOK, timeouts=10**6)
    r = run(v)
    assert r.filled == 0 and r.outcome == "UNRESOLVED_ORDER_STATE"
    assert len(r.keys) == 1  # no taker leg while the maker leg is unknown


def test_replay_of_same_decision_uses_same_keys():
    v = SimVenue(BOOK)
    a = run(v)
    b = run(v)
    assert a.keys == b.keys and len(v.orders) == 2
    assert b.filled == 10  # venue replays the existing orders; no new orders were created


def test_keys_differ_by_leg_and_decision():
    other = Decision("d2", "copy", "tok", "BUY", D(10), D("0.50"), D("0.60"))
    assert len({order_key(DEC, "maker"), order_key(DEC, "take"), order_key(other, "maker")}) == 3


def test_unknown_fee_never_takes():
    r = run(SimVenue(BOOK), params=Params(timeout_seconds=5, taker_fee_rate=None))
    assert r.outcome == "MISSED_UNKNOWN_TAKER_FEE" and r.filled == 0


def test_no_edge_after_fee_is_a_miss():
    d = Decision("d3", "copy", "tok", "BUY", D(10), D("0.50"), D("0.515"))
    r = run(SimVenue(BOOK), decision=d)
    assert r.outcome == "MISSED_EDGE_GONE"


def test_slippage_guard():
    wide = Book(D("0.49"), D("0.56"))
    r = run(SimVenue(wide))
    assert r.outcome == "MISSED_SLIPPAGE_GUARD"


def test_missing_book_is_rejected():
    r = run(SimVenue(Book(None, None)))
    assert r.outcome == "REJECTED" and r.reason == "NO_USABLE_BOOK"


def test_fok_killed_is_missed_not_partial():
    v = SimVenue(BOOK, take_fill=D(3))
    r = run(v)
    assert r.outcome == "MISSED_FOK_KILLED" and r.filled == 0


# ───────────── shortfall DB ─────────────


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    Base.metadata.create_all(engine)
    s = Session(engine, expire_on_commit=False)
    s.add(User(id=1, wallet_address="0x" + "1" * 40))
    s.commit()
    yield s
    s.close()


def test_shortfall_record_is_idempotent_and_summarized(db):
    r = run(SimVenue(BOOK))
    row = record_shortfall(db, user_id=1, decision=DEC, execution=r, mode="limit_then_take")
    again = record_shortfall(db, user_id=1, decision=DEC, execution=r, mode="limit_then_take")
    assert row.id == again.id
    assert len(db.scalars(select(ExecutionShortfall)).all()) == 1
    # Bought at 0.51 vs decision 0.50: 10 * 0.01 + fee.
    assert row.shortfall_usdc == D("0.1") + r.fees


def test_missed_decision_records_missed_cost(db):
    wide = Book(D("0.49"), D("0.56"))
    r = run(SimVenue(wide))
    row = record_shortfall(db, user_id=1, decision=DEC, execution=r, mode="limit_then_take")
    assert row.shares_filled == 0 and row.shortfall_usdc is None
    assert row.missed_shares == 10
    assert row.missed_cost_usdc == (D("0.525") - D("0.50")) * 10
    (s,) = summarize(db, 1)
    assert D(s["fill_rate"]) == 0 and s["orders"] == 1


def test_summary_per_strategy_and_mode(db):
    for i, mode in enumerate(["taker", "limit_then_take"]):
        d = Decision(f"x{i}", "copy", "tok", "BUY", D(10), D("0.50"), D("0.60"))
        record_shortfall(
            db, user_id=1, decision=d, execution=run(SimVenue(BOOK), decision=d), mode=mode
        )
    modes = {s["mode"] for s in summarize(db, 1)}
    assert modes == {"taker", "limit_then_take"}


# ───────────── paper comparison ─────────────


def test_comparison_gives_no_maker_fill_without_evidence():
    case = ComparisonCase(DEC, BOOK, D(10), BOOK, D(10), attributed_maker_shares=None)
    out = compare_modes([case], PARAMS)["arms"]
    assert out["limit_then_take"]["fees_usdc"] == out["fok"]["fees_usdc"]  # all taker, same fee


def test_comparison_credits_maker_only_with_attributed_flow():
    ev = ComparisonCase(DEC, BOOK, D(10), BOOK, D(10), attributed_maker_shares=D(10))
    out = compare_modes([ev], PARAMS)["arms"]
    assert D(out["limit_then_take"]["fees_usdc"]) == 0
    assert D(out["limit_then_take"]["net_value_usdc"]) > D(out["fok"]["net_value_usdc"])


def test_comparison_fok_misses_when_depth_short_and_requires_fee():
    thin = ComparisonCase(DEC, BOOK, D(2), BOOK, D(10))
    out = compare_modes([thin], PARAMS)["arms"]
    assert D(out["fok"]["fill_rate"]) == 0 and D(out["limit_then_take"]["fill_rate"]) == 1
    with pytest.raises(ValueError):
        compare_modes([thin], Params(taker_fee_rate=None))


# ───────────── stop-loss interval ─────────────


def _settings(default=10, short=None):
    return SimpleNamespace(
        stop_loss_check_interval_seconds=default,
        stop_loss_check_interval_short_markets_seconds=short,
    )


def test_interval_defaults_unchanged():
    assert interval_for_titles(["Bitcoin Up or Down - 5m"], _settings()) == 10


def test_interval_short_markets_override():
    s = _settings(short=2)
    assert interval_for_titles(["Bitcoin Up or Down - 5m", "Fed cut?"], s) == 2
    assert interval_for_titles(["Fed cut?", None], s) == 10
    assert interval_for_titles([], s) == 10
    assert interval_for_titles(["x up or down"], _settings(default=1, short=5)) == 1


# ───────────── real gateway binding ─────────────


def test_gateway_venue_places_through_real_gateway_and_replays(gateway):  # noqa: F811
    db_, _, _, submit = gateway
    v = GatewayVenue(
        db_,
        user_id=1,
        strategy="copy",
        book_reader=lambda t: BOOK,
        copied_from_wallet="0x" + "2" * 40,
    )
    kw = {
        "key": "ltt:k1",
        "token_id": "123",
        "side": "BUY",
        "price": D("0.5"),
        "shares": D(10),
        "order_type": "GTC",
        "post_only": True,
    }
    first = v.place(**kw)
    again = v.place(**kw)
    assert first.state == again.state == "open"
    submit.assert_called_once()  # replay of the same key never sends a second order
