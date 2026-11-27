from decimal import Decimal

import pytest

from app.services.trading.btc_paper_decision import (
    Quote,
    blend_with_market,
    brier_score,
    decide,
    eligible_arrival_quote,
    probability,
    proxy_baselines,
    spike_safe_limit,
    stress_reasons,
    visible_buy_estimate,
)


def quotes(*, timestamp=99, available="100", up=".26", down=".75"):
    return {
        side: Quote(Decimal(price), Decimal(".01"), timestamp, Decimal(available), Decimal(5))
        for side, price in (("Up", up), ("Down", down))
    }


def test_original_forecast_checks_both_prices_and_requires_venue_reference():
    decision = decide(Decimal(".45"), quotes(), now=100, close_at=300, reference_verified=False)
    assert (decision.action, decision.reason) == ("HOLD", "MISSING_CHAINLINK_OPEN")
    assert decision.edges == {"Up": Decimal(".1874"), "Down": Decimal("-.2075")}
    decision = decide(Decimal(".45"), quotes(), now=100, close_at=300, reference_verified=True)
    assert decision.action == "Up"
    assert decision.limit == Decimal(".42")


@pytest.mark.parametrize(
    "timestamp, available, close",
    [(70, "100", 300), (101, "100", 300), (99, "4", 300), (99, "100", 119)],
)
def test_stale_future_shallow_and_expiring_quotes_cannot_trade(timestamp, available, close):
    decision = decide(
        Decimal(".45"),
        quotes(timestamp=timestamp, available=available),
        now=100,
        close_at=close,
        reference_verified=True,
    )
    assert decision.action == "HOLD"


def test_positive_gross_edge_below_net_gate_does_not_trade():
    decision = decide(
        Decimal(".52"), quotes(up=".50", down=".51"), now=100, close_at=300, reference_verified=True
    )
    assert decision.action == "HOLD"


def test_scoring_keeps_hold_forecasts_and_fixed_shrinkage():
    assert blend_with_market(Decimal(".45"), Decimal(".25")) == Decimal(".35")
    assert brier_score(Decimal(".45"), up=False) == Decimal(".2025")
    with pytest.raises(ValueError):
        probability("NaN")
    with pytest.raises(ValueError):
        probability(True)


def test_one_sided_market_never_invents_a_quote_for_the_missing_outcome():
    decision = decide(
        Decimal(".45"),
        {"Down": quotes()["Down"]},
        now=100,
        close_at=300,
        reference_verified=True,
    )
    assert decision.action == "HOLD"
    assert decision.edges == {"Down": Decimal("-.2075")}


@pytest.mark.parametrize(
    "p_up, market, up, down",
    [(".77", ".85", ".89", ".15"), (".36", ".33", ".33", ".70")],
)
def test_unvalidated_counter_market_edge_is_observed_without_an_order(p_up, market, up, down):
    decision = decide(
        Decimal(p_up),
        quotes(up=up, down=down),
        now=100,
        close_at=300,
        reference_verified=True,
        market_midpoint=Decimal(market),
    )
    assert decision.action == "HOLD"
    assert decision.reason == "COUNTER_MARKET_EDGE_UNVERIFIED"
    assert max(decision.edges.values()) >= Decimal(".02")


def test_market_direction_filter_does_not_replace_executable_edge_check():
    decision = decide(
        Decimal(".71"),
        quotes(up=".64", down=".37"),
        now=100,
        close_at=300,
        reference_verified=True,
        market_midpoint=Decimal(".63"),
    )
    assert decision.action == "Up"


def test_baselines_are_fixed_and_neutral_without_proxy_movement():
    assert proxy_baselines(Decimal(".001")) == {
        "momentum": Decimal(".60"),
        "mean_reversion": Decimal(".40"),
    }
    assert proxy_baselines(Decimal(0)) == {
        "momentum": Decimal(".50"),
        "mean_reversion": Decimal(".50"),
    }


def test_book_walk_keeps_unfilled_notional_and_respects_price_limit():
    estimate = visible_buy_estimate(
        [
            (Decimal(".55"), Decimal(100)),
            (Decimal(".40"), Decimal(5)),
            (Decimal(".45"), Decimal(4)),
        ],
        Decimal(".45"),
        Decimal(5),
    )
    assert estimate["shares"] == Decimal(9)
    assert estimate["notional"] == Decimal("3.80")
    assert estimate["unfilled_notional"] == Decimal("1.20")
    assert estimate["modeled_fee"] == Decimal(".038")
    assert estimate["vwap"] == Decimal("3.80") / 9


@pytest.mark.parametrize("move", ["15", "-15", "42", "-42"])
def test_spike_gate_is_symmetric(move):
    assert stress_reasons(
        proxy_move_bps=Decimal(move),
        proxy_range_bps=Decimal("12"),
        forecast_midpoint=Decimal(".60"),
        execution_midpoint=Decimal(".61"),
        forecast_ask=None,
        quote=None,
    ) == ("PROXY_SPIKE",)


def test_reversal_and_book_deterioration_block_even_if_net_move_is_flat():
    quote = Quote(Decimal(".65"), Decimal(".01"), 100, Decimal(100), Decimal(5), Decimal(".58"))
    assert stress_reasons(
        proxy_move_bps=Decimal(0),
        proxy_range_bps=Decimal("26"),
        forecast_midpoint=Decimal(".50"),
        execution_midpoint=Decimal(".57"),
        forecast_ask=Decimal(".60"),
        quote=quote,
    ) == ("PROXY_RANGE_SPIKE", "MARKET_REPRICED", "WIDE_SPREAD", "ASK_REPRICED")


def test_quiet_narrow_book_is_not_blocked():
    quote = Quote(Decimal(".62"), Decimal(".01"), 100, Decimal(100), Decimal(5), Decimal(".60"))
    assert not stress_reasons(
        proxy_move_bps=Decimal("-8"),
        proxy_range_bps=Decimal("12"),
        forecast_midpoint=Decimal(".59"),
        execution_midpoint=Decimal(".61"),
        forecast_ask=Decimal(".61"),
        quote=quote,
    )


def test_limit_is_pinned_against_adverse_repricing_during_final_capture():
    quote = Quote(Decimal(".60"), Decimal(".01"), 100, Decimal(100), Decimal(5))
    assert spike_safe_limit(quote, Decimal(".70")) == Decimal(".61")
    assert spike_safe_limit(quote, Decimal(".60")) == Decimal(".60")


def test_execution_latency_requires_book_at_or_after_arrival():
    for at, allowed in [(10, False), (11.99, False), (12, True), (13, True), (14, False)]:
        quote = quotes(timestamp=at)["Up"]
        assert eligible_arrival_quote(quote, decision_at=10, delay_seconds=2, now=13) is allowed


def test_market_fee_can_remove_an_edge_under_the_flat_fee_assumption():
    book = quotes(up=".50", down=".51")
    assert (
        decide(Decimal(".53"), book, now=100, close_at=300, reference_verified=True).action == "Up"
    )
    assert (
        decide(
            Decimal(".53"),
            book,
            now=100,
            close_at=300,
            reference_verified=True,
            venue_taker_rate=Decimal(".05"),
        ).action
        == "HOLD"
    )
