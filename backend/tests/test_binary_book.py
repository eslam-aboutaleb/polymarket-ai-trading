from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.market_data.binary_book import BookEvent, Level, MarketSpec, reconstruct

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def spec(**changes):
    return MarketSpec(
        condition_id="condition",
        yes_token="11",
        no_token="22",
        rule_hash="rules",
        metadata_id="metadata",
        received_at=NOW - timedelta(seconds=1),
        active=True,
        tick=Decimal("0.01"),
        minimum_shares=Decimal("1"),
        **changes,
    )


def snapshot(**changes):
    values = {
        "snapshot_id": "base",
        "token_id": "11",
        "condition_id": "condition",
        "rule_hash": "rules",
        "received_at": NOW,
        "source_at": NOW,
        "connection_id": "one",
        "kind": "snapshot",
        "sequence": 1,
        "sequence_contract": "consecutive",
        "bids": [Level(price="0.4", shares="10")],
        "asks": [Level(price="0.5", shares="10"), Level(price="0.6", shares="20")],
    }
    return BookEvent(**{**values, **changes})


def delta(**changes):
    return snapshot(
        **{
            **{
                "snapshot_id": "delta",
                "received_at": NOW + timedelta(seconds=1),
                "source_at": NOW + timedelta(seconds=1),
                "kind": "delta",
                "sequence": 2,
                "bids": [],
                "asks": [],
                "changes": [("SELL", Level(price="0.5", shares="0"))],
            },
            **changes,
        }
    )


def test_replay_delta_removes_liquidity_and_reproduces_hash():
    result = reconstruct(spec(), "11", [snapshot(), delta()], NOW + timedelta(seconds=2))
    assert result.grade == "VERIFIED_BOOK"
    assert result.asks == [Level(price="0.6", shares="20")]
    assert result.continuous_history_verified
    assert result == reconstruct(
        spec(), "11", [delta(), snapshot(), snapshot()], NOW + timedelta(seconds=2)
    )


@pytest.mark.parametrize(
    "event,reason",
    [
        (delta(sequence=3), "DELTA_CONTINUITY_UNPROVEN"),
        (delta(sequence_contract="unavailable"), "DELTA_CONTINUITY_UNPROVEN"),
        (delta(connection_id="reconnect"), "DELTA_CONTINUITY_UNPROVEN"),
        (delta(source_at=NOW - timedelta(seconds=1)), "OUT_OF_ORDER_SOURCE_EVENT"),
        (delta(checksum="mismatch"), "CHECKSUM_MISMATCH"),
        (delta(condition_id="other"), "TOKEN_CONDITION_OR_RULE_MISMATCH"),
    ],
)
def test_bad_replay_abstains(event, reason):
    result = reconstruct(spec(), "11", [snapshot(), event], NOW + timedelta(seconds=2))
    assert result.grade == "PARTIAL" and reason in result.reasons
    assert result.asks == [] and result.needs_resnapshot


def test_crossed_empty_stale_and_resnapshot():
    for event, reason in [
        (snapshot(bids=[]), "EMPTY_BOOK_SIDE"),
        (snapshot(bids=[Level(price="0.7", shares="1")]), "CROSSED_BOOK"),
    ]:
        result = reconstruct(spec(), "11", [event], NOW)
        assert result.grade == "PARTIAL" and reason in result.reasons
    assert reconstruct(spec(), "11", [snapshot()], NOW + timedelta(seconds=61)).grade == "STALE"
    restored = snapshot(
        snapshot_id="restored",
        sequence=4,
        source_at=NOW + timedelta(seconds=3),
        received_at=NOW + timedelta(seconds=3),
    )
    assert (
        reconstruct(
            spec(), "11", [snapshot(), delta(sequence=3), restored], NOW + timedelta(seconds=4)
        ).grade
        == "VERIFIED_BOOK"
    )


def test_wrong_token_future_and_halt():
    assert reconstruct(spec(), "22", [snapshot()], NOW).grade == "NO_HISTORY"
    assert (
        reconstruct(spec(), "11", [snapshot()], NOW - timedelta(microseconds=1)).grade
        == "NO_HISTORY"
    )
    halted = spec().model_copy(update={"active": False})
    assert "HALTED_OR_CLOSED_MARKET" in reconstruct(halted, "11", [snapshot()], NOW).reasons


def test_buy_sell_size_curves_and_fees_are_exact_and_bounded():
    from app.services.market_data.binary_book import WalkRequest, walk_depth

    market = spec()
    book = reconstruct(market, "11", [snapshot()], NOW)
    sizes = [Decimal("5"), Decimal("10"), Decimal("20"), Decimal("40")]
    estimates = [
        walk_depth(
            market,
            book,
            WalkRequest(side="BUY", amount=size, fee_bps="100", fee_version="explicit-flat-test"),
        )
        for size in sizes
    ]
    assert [r.matched_shares for r in estimates] == list(map(Decimal, [5, 10, 20, 30]))
    assert estimates[2].gross_notional == 11
    assert estimates[2].vwap == Decimal("0.55")
    assert estimates[2].fee == Decimal("0.11") and estimates[2].net_cash == Decimal("11.11")
    assert estimates[3].partial and estimates[3].displayed_capacity == 30
    assert all(a.vwap <= b.vwap for a, b in zip(estimates, estimates[1:]))
    sold = walk_depth(
        market, book, WalkRequest(side="SELL", amount="5", fee_bps="100", fee_version="test")
    )
    assert sold.net_cash == Decimal("1.98")
    assert sold.fill_probability is sold.measured_price_impact is None
    budget = walk_depth(
        market,
        book,
        WalkRequest(side="BUY", units="USDC", amount="10", fee_bps="100", fee_version="test"),
    )
    assert budget.net_cash <= 10 and budget.matched_shares <= 30


def test_missing_fees_and_bad_books_never_quote_costs():
    from app.services.market_data.binary_book import WalkRequest, walk_depth

    book = reconstruct(spec(), "11", [snapshot()], NOW)
    assert (
        walk_depth(spec(), book, WalkRequest(side="BUY", amount="10")).reason
        == "FEE_POLICY_UNKNOWN"
    )
    stale = reconstruct(spec(), "11", [snapshot()], NOW + timedelta(seconds=61))
    assert (
        walk_depth(
            spec(), stale, WalkRequest(side="BUY", amount="10", fee_bps="0", fee_version="test")
        ).vwap
        is None
    )


def test_complement_inequalities_do_not_net_token_liquidity():
    from app.services.market_data.binary_book import complement_summary

    yes = reconstruct(spec(), "11", [snapshot()], NOW)
    no = reconstruct(
        spec(),
        "22",
        [
            snapshot(
                token_id="22",
                snapshot_id="no",
                bids=[Level(price="0.2", shares="1")],
                asks=[Level(price="0.3", shares="1")],
            )
        ],
        NOW,
    )
    result = complement_summary(yes, no)
    assert result["ask_sum"] == "0.8" and result["netting_permitted"] is False
    assert result["arbitrage_claim"] is False and result["flags"]
