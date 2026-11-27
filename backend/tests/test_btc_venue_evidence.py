from decimal import Decimal

import pytest

from app.services.trading.btc_venue_evidence import (
    reported_reference,
    resolved_reference_side,
    taker_coefficient,
    taker_fee,
)


def test_fee_is_nonlinear_and_bound_to_the_market_tokens():
    market = {
        "t": [{"t": "10", "o": "Up"}, {"t": "20", "o": "Down"}],
        "fd": {"r": "0.05", "e": 1, "to": True},
    }
    rate = taker_coefficient(market, {"Up": "10", "Down": "20"})
    assert taker_fee(Decimal(".50"), Decimal(100), rate) == Decimal("1.25")
    assert taker_fee(Decimal(".30"), Decimal(100), rate) == Decimal("1.05")
    with pytest.raises(ValueError, match="identity"):
        taker_coefficient(market, {"Up": "20", "Down": "10"})
    market["fd"]["e"] = 2
    with pytest.raises(ValueError, match="Unsupported"):
        taker_coefficient(market, {"Up": "10", "Down": "20"})


def test_only_completed_reference_can_verify_resolution_and_tie_goes_up():
    payload = {"timestamp": 400000, "openPrice": "60100", "closePrice": "60100", "completed": False}
    assert resolved_reference_side(reported_reference(payload, 100, 401)) is None
    payload["completed"] = True
    assert resolved_reference_side(reported_reference(payload, 100, 401)) == "Up"
    assert (
        resolved_reference_side(reported_reference({**payload, "closePrice": 60099}, 100, 401))
        == "Down"
    )
    with pytest.raises(ValueError, match="timestamp"):
        reported_reference(payload, 100, 399)
