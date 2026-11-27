import pytest

from app.services.trading.btc_twap_forecast import settlement_probability

HISTORY = [100, 101, 102, 100, 99, 100, 101, 100, 100.5]


def predict(latest, *, seconds=180, history=HISTORY):
    return settlement_probability(
        proxy_open=100,
        proxy_latest=latest,
        closed_proxy_prices=history,
        seconds_remaining=seconds,
    )


def test_probabilities_are_neutral_at_strike_and_symmetric_around_it():
    assert predict(100) == 0.5
    assert predict(101) > 0.5
    assert predict(101) == pytest.approx(1 - predict(99))


def test_volatility_or_incomplete_averaging_window_cannot_fabricate_confidence():
    assert predict(101, history=[100] * 10) is None
    assert predict(101, history=HISTORY[:4]) is None
    assert predict(101, seconds=59) is None
    assert predict(float("nan")) is None
    assert predict(101, seconds=60) > predict(101, seconds=240)
