"""TWAP-aware edge model: real 5m windows (Binance 1 s closes + resolved outcomes)."""

import gzip
import json
import math
from pathlib import Path

import pytest

from app.services.market_data import binance_ws_service as bws
from app.services.trading import latency_arb_service as svc

FIXTURE = Path(__file__).parent / "fixtures" / "binance_1s_btc5m_resolved_windows.json.gz"
PRE = 420
WINDOW = 300


@pytest.fixture(scope="module")
def windows():
    with gzip.open(FIXTURE, "rt") as handle:
        return json.load(handle)["windows"]


def _reference(w):
    sec = w["sec"]
    return sum(sec[PRE - 60 : PRE]) / 60.0


def _final_mean(w):
    sec = w["sec"]
    return sum(sec[PRE + WINDOW - 60 : PRE + WINDOW]) / 60.0


def _model_p(w, elapsed):
    sec = w["sec"]
    sigma_s = svc.sigma_per_second(w["m1"], 1.5)
    final_start = WINDOW - 60
    observed = sec[PRE + final_start : PRE + elapsed + 1] if elapsed >= final_start else []
    return svc.twap_probability_up(
        _reference(w), sec[PRE + elapsed], elapsed, WINDOW, observed, sigma_s
    )


def _spot_p(w, elapsed):
    sec = w["sec"]
    distance = svc.compute_distance(sec[PRE + elapsed], sec[PRE])
    sigma = max(svc.realized_volatility(w["m1"], 5), svc.MIN_SIGMA)
    return svc.probability_up(distance, sigma, (WINDOW - elapsed) / WINDOW)


def test_resolution_rule_matches_real_outcomes(windows):
    hits = sum((_final_mean(w) >= _reference(w)) == bool(w["up_win"]) for w in windows)
    assert hits / len(windows) >= 0.96


def test_twap_model_beats_spot_model_on_real_windows(windows):
    for elapsed in (120, 200, 240):
        twap = [(_model_p(w, elapsed), w["up_win"]) for w in windows]
        spot = [(_spot_p(w, elapsed), w["up_win"]) for w in windows]
        brier_twap = sum((p - y) ** 2 for p, y in twap) / len(twap)
        brier_spot = sum((p - y) ** 2 for p, y in spot) / len(spot)
        assert brier_twap < brier_spot, (elapsed, brier_twap, brier_spot)


def test_confident_predictions_are_right_on_real_windows(windows):
    confident = []
    for w in windows:
        p = _model_p(w, 240)
        if p >= 0.85 or p <= 0.15:
            confident.append((p >= 0.5) == bool(w["up_win"]))
    assert len(confident) >= 20
    assert sum(confident) / len(confident) >= 0.80


def test_probability_is_monotone_in_price(windows):
    w = windows[0]
    sigma_s = svc.sigma_per_second(w["m1"], 1.5)
    ref = _reference(w)
    ps = [
        svc.twap_probability_up(ref, ref * (1 + d), 100, WINDOW, [], sigma_s)
        for d in (-0.002, -0.0005, 0.0, 0.0005, 0.002)
    ]
    assert ps == sorted(ps)
    assert 0.0 < ps[0] < ps[-1] < 1.0
    assert abs(ps[2] - 0.5) < 1e-9


def test_fully_observed_final_minute_is_deterministic():
    ref = 100.0
    up = svc.twap_probability_up(ref, 100.2, 300, 300, [100.2] * 60, 1e-4)
    down = svc.twap_probability_up(ref, 99.8, 300, 300, [99.8] * 60, 1e-4)
    assert up == 1.0 and down == 0.0


def test_observed_seconds_pin_the_average():
    # 45 of 60 final seconds already well above the reference: nearly settled.
    p = svc.twap_probability_up(100.0, 100.0, 285, 300, [100.5] * 45, 1e-4)
    assert p > 0.99


def test_sigma_per_second_needs_history_and_caps_spikes():
    assert svc.sigma_per_second([100.0] * 5) == 0.0
    calm = [100.0 * (1 + 0.0002 * math.sin(i)) for i in range(120)]
    spiky = calm[:-3] + [101.0, 99.0, 102.0]
    capped = svc.sigma_per_second(spiky, 1.5)
    uncapped = svc.sigma_per_second(spiky, 1000.0)
    assert 0.0 < capped <= uncapped


def test_second_price_buffer_carries_forward_and_rejects_gaps():
    state = bws.SymbolState(symbol="TESTUSDT")
    bws._state["TESTUSDT"] = state
    try:
        for sec, price in ((1000, 10.0), (1001, 11.0), (1004, 14.0)):
            state.record_trade(price, sec * 1000 + 500)
        assert bws.get_second_prices("TESTUSDT", 1000, 1004) == [10.0, 11.0, 11.0, 11.0, 14.0]
        assert bws.get_second_prices("TESTUSDT", 900, 1004) is None  # nothing before start
        assert bws.get_second_prices("TESTUSDT", 1000, 1030) is None  # gap > 10 s
        assert bws.get_second_prices("TESTUSDT", 1004, 1000) is None
    finally:
        bws._state.pop("TESTUSDT", None)


def test_second_price_buffer_is_bounded():
    state = bws.SymbolState(symbol="TESTUSDT")
    for i in range(bws.SECOND_HISTORY_SECONDS + 500):
        state.record_trade(1.0 + i, (10_000 + i) * 1000)
    assert len(state.second_prices) <= bws.SECOND_HISTORY_SECONDS + 61


def test_evaluate_opportunity_uses_twap_and_reports_model(windows, monkeypatch):
    from datetime import UTC, datetime

    from app.services.market_data.crypto_markets_service import CryptoMarket

    w = windows[5]
    elapsed = 200
    slot = w["slot"]
    market = CryptoMarket(
        symbol="BTC",
        window_minutes=5,
        window_start_epoch=slot,
        condition_id="c",
        question="q",
        token_ids={"up": "u", "down": "d"},
        prices={"up": 0.5, "down": 0.5},
    )
    now = datetime.fromtimestamp(slot + elapsed, tz=UTC)
    sec = w["sec"]
    args = (
        "BTC",
        5,
        market,
        sec[PRE + elapsed],
        sec[PRE],
        now,
        w["m1"],
    )
    twap = svc.evaluate_opportunity(
        *args, edge_threshold=0.0, taker_fee_rate=0.0, twap_reference=_reference(w), final_prices=[]
    )
    assert twap is not None and twap.model == "twap"
    assert twap.p_model == pytest.approx(_model_p(w, elapsed))
    spot = svc.evaluate_opportunity(*args, edge_threshold=0.0, taker_fee_rate=0.0)
    assert spot is not None and spot.model == "spot"
    assert twap.to_dict()["model"] == "twap"


def _opp(windows, **setting_overrides):
    from datetime import UTC, datetime

    from app.config import get_settings
    from app.services.market_data.crypto_markets_service import CryptoMarket

    w = windows[5]
    elapsed = 200
    market = CryptoMarket(
        symbol="BTC",
        window_minutes=5,
        window_start_epoch=w["slot"],
        condition_id="c",
        token_ids={"up": "u", "down": "d"},
        prices={"up": 0.5, "down": 0.5},
    )
    now = datetime.fromtimestamp(w["slot"] + elapsed, tz=UTC)
    sec = w["sec"]
    settings = get_settings().model_copy(update=setting_overrides)
    from unittest.mock import patch

    with patch.object(svc, "get_settings", lambda: settings):
        return svc.evaluate_opportunity(
            "BTC",
            5,
            market,
            sec[PRE + elapsed],
            sec[PRE],
            now,
            w["m1"],
            edge_threshold=0.0,
            taker_fee_rate=0.0,
            twap_reference=_reference(w),
            final_prices=[],
        )


def test_filters_are_off_by_default(windows):
    assert _opp(windows) is not None


def test_min_own_probability_filter(windows):
    base = _opp(windows)
    own = base.p_model if base.side == "up" else 1.0 - base.p_model
    assert _opp(windows, latency_arb_min_own_probability=own - 0.01) is not None
    assert _opp(windows, latency_arb_min_own_probability=min(own + 0.01, 0.999)) is None


def test_leg_price_band_filter(windows):
    # The fixture market is 50/50, so the leg price is 0.50.
    assert _opp(windows, latency_arb_leg_price_min=0.2, latency_arb_leg_price_max=0.85) is not None
    assert _opp(windows, latency_arb_leg_price_min=0.6, latency_arb_leg_price_max=0.9) is None
    assert _opp(windows, latency_arb_leg_price_min=0.1, latency_arb_leg_price_max=0.4) is None


def test_filter_settings_are_validated():
    from pydantic import ValidationError

    from app.config import get_settings

    base = get_settings().model_dump()
    for bad in (
        {"latency_arb_min_own_probability": 1.0},
        {"latency_arb_leg_price_min": 0.9, "latency_arb_leg_price_max": 0.5},
        {"latency_arb_edge_model": "nope"},
    ):
        with pytest.raises((ValidationError, ValueError)):
            type(get_settings())(**{**base, **bad})


def test_pick_edge_threshold_is_walk_forward():
    good = [(0.18, 1.0, 10.0)] * 150 + [(0.12, -1.0, 10.0)] * 150
    # >= 0.20: no rows; >= 0.15: positive; >= 0.10: breaks even-ish negative.
    assert svc.pick_edge_threshold(good, grid=(0.10, 0.15, 0.20), min_trades=100) == 0.15
    assert svc.pick_edge_threshold([], min_trades=100, default=0.15) == 0.15
    losing = [(0.3, -1.0, 10.0)] * 200
    assert svc.pick_edge_threshold(losing, min_trades=100, default=0.2) == 0.2


def test_walk_forward_script_uses_only_past_days(tmp_path, capsys, monkeypatch):
    import importlib.util
    import sys

    csv_path = tmp_path / "s.csv"
    rows = ["day,edge,pnl,stake"]
    rows += ["2026-11-01,0.20,1.0,10.0" for _ in range(120)]
    rows += ["2026-11-02,0.20,-9.0,10.0" for _ in range(120)]
    csv_path.write_text("\n".join(rows))
    script = Path(__file__).parents[1] / "scripts" / "latency_arb_walk_forward.py"
    spec = importlib.util.spec_from_file_location("lawf", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys, "argv", ["x", str(csv_path)])
    assert mod.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "2026-11-01 threshold=0.15"  # no history yet: default
    assert (
        out[1] == "2026-11-02 threshold=0.10"
    )  # day 1 only: first positive grid value is best ROI
