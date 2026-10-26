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
            "snapshot_id": "delta",
            "received_at": NOW + timedelta(seconds=1),
            "source_at": NOW + timedelta(seconds=1),
            "kind": "delta",
            "sequence": 2,
            "bids": [],
            "asks": [],
            "changes": [("SELL", Level(price="0.5", shares="0"))],
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
    assert all(a.vwap <= b.vwap for a, b in zip(estimates, estimates[1:], strict=False))
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


@pytest.mark.parametrize("amount", ["1", "1.001", "9.99", "10", "10.01", "29.99", "30", "1000"])
def test_depth_never_exceeds_displayed_quantity_or_budget(amount):
    from app.services.market_data.binary_book import WalkRequest, walk_depth

    book = reconstruct(spec(), "11", [snapshot()], NOW)
    request = WalkRequest(side="BUY", units="USDC", amount=amount, fee_bps="25", fee_version="test")
    result = walk_depth(spec(), book, request)
    if result.status == "modeled":
        assert result.matched_shares <= 30 and result.net_cash <= Decimal(amount)
        assert all(point.shares > 0 for point in result.curve)
        assert all(
            a.cumulative_shares < b.cumulative_shares
            for a, b in zip(result.curve, result.curve[1:], strict=False)
        )


def test_simultaneous_ambiguous_updates_and_identity_collisions_abstain():
    events = [
        snapshot(sequence_contract="unavailable"),
        snapshot(
            snapshot_id="other",
            sequence_contract="unavailable",
            asks=[Level(price="0.7", shares="5")],
        ),
    ]
    assert reconstruct(spec(), "11", events, NOW).grade == "PARTIAL"
    with pytest.raises(ValueError, match="IDENTITY_CONFLICT"):
        reconstruct(spec(), "11", [snapshot(), snapshot(asks=[])], NOW)


def test_ambient_decimal_precision_cannot_change_results():
    from decimal import localcontext

    from app.services.market_data.binary_book import WalkRequest, walk_depth

    baseline = reconstruct(spec(), "11", [snapshot()], NOW)
    request = WalkRequest(side="BUY", amount="20", fee_bps="100", fee_version="test")
    with localcontext() as context:
        context.prec = 4
        assert reconstruct(spec(), "11", [snapshot()], NOW) == baseline
        assert walk_depth(spec(), baseline, request).vwap == Decimal("0.55")


def test_round_trip_spread_fees_and_queue_boundary():
    from app.services.market_data.binary_book import (
        NoQueueEvidencePolicy,
        WalkRequest,
        round_trip_cost,
    )

    book = reconstruct(spec(), "11", [snapshot()], NOW)
    result = round_trip_cost(
        spec(), book, WalkRequest(side="BUY", amount="5", fee_bps="100", fee_version="test")
    )
    assert Decimal(result["entry_cost"]) == Decimal("2.525")
    assert Decimal(result["exit_proceeds"]) == Decimal("1.98")
    assert Decimal(result["modeled_spread_and_fee_cost"]) == Decimal("0.545")
    assert NoQueueEvidencePolicy().assess(book).fill_probability is None


def test_tick_minimum_size_limit_and_fee_sensitivity():
    from app.services.market_data.binary_book import WalkRequest, walk_depth

    market = spec()
    book = reconstruct(market, "11", [snapshot()], NOW)
    request = WalkRequest(side="BUY", amount="20", fee_bps="0", fee_version="test")
    limited = walk_depth(market, book, request.model_copy(update={"limit_price": Decimal("0.55")}))
    assert limited.matched_shares == 10 and limited.partial
    assert (
        walk_depth(
            market, book, request.model_copy(update={"limit_price": Decimal("0.555")})
        ).reason
        == "LIMIT_OFF_TICK"
    )
    assert (
        walk_depth(market, book, request.model_copy(update={"amount": Decimal("0.999")})).reason
        == "BELOW_MINIMUM_ORDER_SIZE"
    )
    assert (
        walk_depth(market, book, request.model_copy(update={"fee_bps": Decimal("100")})).net_cash
        > walk_depth(market, book, request).net_cash
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"source_at": None}, "SOURCE_CLOCK_UNKNOWN_OR_FUTURE"),
        ({"source_at": NOW + timedelta(seconds=1)}, "SOURCE_CLOCK_UNKNOWN_OR_FUTURE"),
        ({"asks": [Level(price="0.505", shares="1")]}, "OFF_TICK_LEVEL"),
        ({"checksum": "incomplete"}, "CHECKSUM_UNVERIFIABLE"),
        ({"rule_hash": "changed"}, "TOKEN_CONDITION_OR_RULE_MISMATCH"),
    ],
)
def test_unusable_source_fields_reject_costs(changes, reason):
    book = reconstruct(spec(), "11", [snapshot(**changes)], NOW)
    assert book.grade == "PARTIAL" and reason in book.reasons
    assert book.asks == []
