"""Fee-aware net-edge gate for taker strategies (issue #154).

Fees come from the repository's own ``archived_taker_fee``. The gate's inputs
(fee metadata and order books) are plain data shaped like the Gamma and CLOB
payloads, passed through the gates' injectable fetchers. No network is used.
"""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.market_data.crypto_markets_service import CryptoMarket
from app.services.market_data.valuation_fees import archived_taker_fee
from app.services.trading import copy_trade_service, inverse_bot_monitor, latency_arb_service
from app.services.trading import net_edge as ne
from app.services.trading.latency_arb_service import Opportunity

D = Decimal
pytestmark = pytest.mark.real_net_edge


def metadata(rate=0.07, **gamma_overrides):
    gamma = {
        "version": "v1",
        "feesEnabled": True,
        "feeType": "crypto_fees_v2",
        "feeSchedule": {"exponent": 1, "rate": rate, "takerOnly": True},
    }
    gamma.update(gamma_overrides)
    return {"clob": {}, "gamma": gamma}


def book(bid="0.49", ask="0.51", size="1000"):
    return ne.BookSide(bids=(ne.Level(D(bid), D(size)),), asks=(ne.Level(D(ask), D(size)),))


def fetchers(meta, the_book):
    async def fee(_condition_id):
        return meta

    async def get_book(_token_id):
        return the_book

    return fee, get_book


def opportunity(side="up", p_model=0.7):
    now = datetime.now(UTC)
    start = int(now.timestamp()) - (int(now.timestamp()) % 300)
    market = CryptoMarket(
        symbol="BTC",
        window_minutes=5,
        window_start_epoch=start,
        condition_id="cond-btc-5m",
        token_ids={"up": "token-up", "down": "token-down"},
        prices={"up": 0.5, "down": 0.5},
        question="Bitcoin up or down in 5 minutes?",
    )
    return Opportunity(
        symbol="BTC",
        window_minutes=5,
        window_start_epoch=start,
        side=side,
        p_model=p_model,
        p_market=0.5,
        edge=0.1,
        distance=0.01,
        t_remaining=0.5,
        sigma=0.01,
        current_price=67000.0,
        window_open=67000.0,
        market=market,
        detected_at=now,
        feed_lag_ms=50.0,
    )


# ── fee ───────────────────────────────────────────────────────────────────


def test_fee_is_higher_near_half_than_near_ninety_and_matches_valuation_fees():
    near_half = ne.taker_fee_per_share(D("0.50"), metadata())
    near_ninety = ne.taker_fee_per_share(D("0.90"), metadata())
    assert near_half > near_ninety
    for price, got in ((D("0.50"), near_half), (D("0.90"), near_ninety)):
        expected, _ = archived_taker_fee(metadata(), [{"price": str(price), "shares": "1"}])
        assert got == expected
    assert near_half == D("0.01750")
    assert near_ninety == D("0.00630")


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {},
        {"clob": {}},
        metadata(feeType="something_else"),
        metadata(feeSchedule={}),
        metadata(version="v2"),
        {"clob": {}, "gamma": {"feesEnabled": None}},
    ],
)
def test_unknown_or_unsupported_fee_metadata_abstains_never_zero(meta):
    with pytest.raises(ne.Abstain):
        ne.taker_fee_per_share(D("0.5"), meta)
    decision = ne.net_edge(
        D("0.9"), [ne.Leg("BUY", D("0.5"), D("10"), meta, book())], margin=D("0.01")
    )
    assert decision.allowed is False
    assert decision.net_edge is None
    assert decision.reason.startswith("ABSTAIN")


def test_explicit_venue_zero_fee_is_priced_zero_and_contradiction_abstains():
    zero = {"clob": {}, "gamma": {"feesEnabled": False}}
    assert ne.taker_fee_per_share(D("0.5"), zero) == 0
    contradictory = {"clob": {"taker_base_fee": 100}, "gamma": {"feesEnabled": False}}
    with pytest.raises(ne.Abstain):
        ne.taker_fee_per_share(D("0.5"), contradictory)


# ── arithmetic ─────────────────────────────────────────────────────────────


def test_decision_carries_every_cost_term_and_blocks_non_positive_net_edge():
    leg = ne.Leg("BUY", D("0.51"), D("100"), metadata(), book())
    fee = ne.taker_fee_per_share(D("0.51"), metadata())
    ok = ne.net_edge(D("0.10"), [leg], margin=D("0.02"))
    assert ok.fee == fee
    assert ok.half_spread == D("0.01")
    assert ok.slippage == 0
    assert ok.net_edge == D("0.10") - fee - D("0.01") - D("0.02")
    assert ok.allowed and ok.reason == "OK"
    # gross edge equal to total cost: net is exactly zero and not tradable
    flat = ne.net_edge(fee + D("0.01") + D("0.02"), [leg], margin=D("0.02"))
    assert flat.net_edge == 0
    assert flat.allowed is False
    assert flat.reason == "NET_EDGE_NOT_POSITIVE"
    below = ne.net_edge(D("0.10"), [leg], margin=D("0.02"), min_net_edge=D("0.5"))
    assert below.allowed is False and below.reason == "NET_EDGE_BELOW_MINIMUM"


def test_slippage_walks_the_ladder_and_thin_depth_abstains():
    ladder = ne.BookSide(
        bids=(ne.Level(D("0.49"), D("1000")),),
        asks=(ne.Level(D("0.50"), D("10")), ne.Level(D("0.60"), D("10"))),
    )
    assert ne.expected_slippage("BUY", ladder, D("20")) == D("0.05")
    with pytest.raises(ne.Abstain, match="INSUFFICIENT_DEPTH"):
        ne.expected_slippage("BUY", ladder, D("21"))
    with pytest.raises(ne.Abstain, match="SPREAD_UNKNOWN"):
        ne.half_spread(ne.BookSide())


def test_parse_book_sorts_levels_and_rejects_malformed_rows():
    parsed = ne.parse_book(
        {
            "bids": [{"price": "0.40", "size": "5"}, {"price": "0.45", "size": "7"}],
            "asks": [{"price": "0.60", "size": "5"}, {"price": "0.55", "size": "7"}],
        }
    )
    assert parsed.bids[0].price == D("0.45")
    assert parsed.asks[0].price == D("0.55")
    with pytest.raises(ne.Abstain):
        ne.parse_book({"bids": [["0.4", "5"]]})


# ── latency arb ─────────────────────────────────────────────────────────────


def test_latency_arb_blocks_thin_edge_unknown_fee_and_missing_book():
    fee, get_book = fetchers(metadata(), book())

    def gate(opp, fee_fn, book_fn):
        return asyncio.run(
            latency_arb_service._net_edge_gate(
                opp, "tok", 50.0, fee_fetcher=fee_fn, book_fetcher=book_fn
            )
        )

    assert gate(opportunity(p_model=0.70), fee, get_book).allowed is True
    thin = gate(opportunity(p_model=0.54), fee, get_book)
    assert thin.allowed is False and thin.reason == "NET_EDGE_NOT_POSITIVE"
    no_fee, get_book2 = fetchers(None, book())
    unknown = gate(opportunity(p_model=0.9), no_fee, get_book2)
    assert unknown.allowed is False and unknown.reason == "ABSTAIN_FEE_METADATA_MISSING"
    fee3, no_book = fetchers(metadata(), None)
    missing = gate(opportunity(p_model=0.9), fee3, no_book)
    assert missing.allowed is False and missing.reason == "ABSTAIN_BOOK_UNAVAILABLE"


def test_latency_arb_down_side_uses_complement_probability():
    fee, get_book = fetchers(metadata(), book())
    decision = asyncio.run(
        latency_arb_service._net_edge_gate(
            opportunity(side="down", p_model=0.30),
            "tok",
            50.0,
            fee_fetcher=fee,
            book_fetcher=get_book,
        )
    )
    assert decision.gross_edge == D("0.7") - D("0.51")
    assert decision.allowed is True


# ── inverse bot ─────────────────────────────────────────────────────────────


def _reversal(held, alt, gross, meta=None, rate=0.07):
    fee, get_book = fetchers(metadata(rate) if meta is None else meta, book())
    return asyncio.run(
        inverse_bot_monitor._reversal_net_edge_gate(
            condition_id="c",
            held_token_id="held",
            held_price=held,
            alt_token_id="alt",
            alt_price=alt,
            shares=100.0,
            gross_edge=gross,
            fee_fetcher=fee,
            book_fetcher=get_book,
        )
    )


def test_reversal_prices_both_legs():
    decision = _reversal(0.30, 0.70, 0.40)
    assert [leg.side for leg in decision.legs] == ["SELL", "BUY"]
    exit_fee = ne.taker_fee_per_share(D("0.3"), metadata())
    entry_fee = ne.taker_fee_per_share(D("0.7"), metadata())
    assert decision.fee == exit_fee + entry_fee
    assert decision.half_spread == D("0.02")  # one half spread per leg
    assert decision.net_edge == D("0.40") - decision.fee - D("0.02") - D("0.02")
    assert decision.allowed


def test_reversal_with_small_gap_is_blocked_once_both_legs_are_paid():
    decision = _reversal(0.46, 0.54, 0.05)
    assert decision.allowed is False
    assert decision.net_edge < 0


def test_reversal_with_unknown_fee_metadata_abstains():
    fee, get_book = fetchers(None, book())
    decision = asyncio.run(
        inverse_bot_monitor._reversal_net_edge_gate(
            condition_id="c",
            held_token_id="held",
            held_price=0.3,
            alt_token_id="alt",
            alt_price=0.7,
            shares=100.0,
            gross_edge=0.4,
            fee_fetcher=fee,
            book_fetcher=get_book,
        )
    )
    assert decision.allowed is False
    assert decision.reason == "ABSTAIN_FEE_METADATA_MISSING"


# ── copy trade ───────────────────────────────────────────────────────────────


def _copy(side, price, assessment, meta=None):
    fee, get_book = fetchers(metadata() if meta is None else meta, book())
    return asyncio.run(
        copy_trade_service._copy_net_edge_gate(
            market_id="m",
            token_id="t",
            side=side,
            price=price,
            trade_size=50.0,
            assessment=assessment,
            fee_fetcher=fee,
            book_fetcher=get_book,
        )
    )


def test_copy_buy_needs_a_probability_and_positive_net_edge():
    no_prob, gated = _copy("BUY", 0.51, {"recommendation": "copy"})
    assert gated and not no_prob.allowed
    assert no_prob.reason == "ABSTAIN_GROSS_EDGE_UNKNOWN"
    good, _ = _copy("BUY", 0.51, {"ai_probability": 75})
    assert good.allowed and good.gross_edge == D("0.75") - D("0.51")
    thin, _ = _copy("BUY", 0.51, {"ai_probability": 0.53})
    assert not thin.allowed and thin.reason == "NET_EDGE_NOT_POSITIVE"
    unknown, _ = _copy("BUY", 0.51, {"ai_probability": 0.9}, meta={})
    assert not unknown.allowed and unknown.reason == "ABSTAIN_FEE_METADATA_MISSING"


def test_copy_sell_is_priced_for_the_log_but_never_gated():
    decision, gated = _copy("SELL", 0.51, None)
    assert gated is False
    assert decision.allowed is False  # recorded as abstain, exit still proceeds


# ── audit log ────────────────────────────────────────────────────────────────


def test_every_decision_is_logged_with_all_terms(caplog):
    decision = ne.net_edge(
        D("0.2"), [ne.Leg("BUY", D("0.51"), D("10"), metadata(), book())], margin=D("0.02")
    )
    with caplog.at_level("INFO"):
        ne.log_decision("latency_arb", decision, symbol="BTC")
    line = next(
        r.getMessage() for r in caplog.records if "taker_net_edge_decision" in r.getMessage()
    )
    body = json.loads(line.split("taker_net_edge_decision ", 1)[1])
    assert body["strategy"] == "latency_arb"
    for key in ("gross_edge", "fee", "half_spread", "slippage", "margin", "net_edge"):
        assert key in body


# ── Nov 13 capture replay ─────────────────────────────────────────────────────


def test_nov_13_capture_replay_every_reconstructed_forecast_is_skipped():
    """Apply the gate to the 5 forecasts in the committed replay report.

    These are reconstructed frequency-benchmark forecasts, not historical bot
    decisions. Spread is not in the report, so costs here are a lower bound.
    """
    from pathlib import Path

    report = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "user guide/acceptance-replay-2026-11-13/report.json"
        ).read_text()
    )
    forecasts = report["forecasts"]
    assert len(forecasts) == 5
    skipped = 0
    for f in forecasts:
        gross = D(f["p_reconstructed"]) - D(f["vwap_ten_shares"])
        decision = ne.net_edge(
            gross,
            [ne.Leg("BUY", D(f["vwap_ten_shares"]), D(10), metadata(), book())],
            margin=D("0.02"),
        )
        skipped += not decision.allowed
    assert skipped == 5
