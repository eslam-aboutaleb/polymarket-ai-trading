from decimal import Decimal

import pytest
from test_btc_book_replay import BOOK, capture

from app.services.trading.btc_book_replay import replay
from app.services.trading.btc_regime_scorecard import regime, scorecard
from app.services.trading.btc_venue_evidence import reported_reference, resolved_reference_side


@pytest.mark.parametrize(
    "move, span, expected",
    [
        (0, 5, "quiet"),
        (13, 14, "rising"),
        (-13, 14, "falling"),
        (-5, 30, "reversal"),
        (None, 8, "unknown"),
        (15, 5, "unknown"),
    ],
)
def test_regime_uses_predecision_evidence(move, span, expected):
    assert regime(move, span) == expected


def test_reference_replay_and_scorecard_keep_estimates_separate_from_fills():
    book = capture((1000, BOOK), (1400, "end"))
    result = replay(book, token="up", arrival_ms=1200, limit=Decimal(".40"), notional=Decimal(5))
    ref = reported_reference(
        {"openPrice": "60001", "closePrice": "60000", "completed": True, "timestamp": 300000},
        0,
        301,
    )
    assert resolved_reference_side(ref) == "Down"
    rows = [
        {"kind": "batch", "start": 0, "windows": 3},
        {
            "kind": "inputs",
            "start": 0,
            "evidence": {"proxy": {"same_source_return_bps": "2", "same_source_range_bps": "3"}},
        },
        {"kind": "forecast", "start": 0, "forecasts": {"market": ".6", "proxy_twap": ".4"}},
        {"kind": "score", "start": 0, "scores": {"market": ".36", "proxy_twap": ".16"}},
        {"kind": "score", "start": 300, "scores": {"market": ".04"}},
        {"kind": "market_replay", "start": 0, "result": result},
        {"kind": "venue_reference_close", "start": 0, "matches": True},
    ]
    report = scorecard(rows)["overall"]
    assert report["windows"] == 3 and report["scored"] == 2
    assert report["fills"] == 0 and report["paper_net_pnl"] is None
    assert report["replay_status"] == {"modeled": 1}
    assert report["reference_checks"] == {"match": 1}
    assert report["forecast_skill"]["proxy_twap"]["matched_market_brier"] == Decimal(".36")


def test_drawdown_distinguishes_pending_positions_and_missing_fee_evidence():
    rows = [
        {"kind": "execution", "start": start, "order": {"status": "partial", "fee_usdc": ".1"}}
        for start in [0, 300, 600]
    ]
    rows.extend(
        [
            {"kind": "score", "start": 300, "scores": {}, "modeled_settlement_pnl": "-3"},
            {
                "kind": "score",
                "start": 0,
                "scores": {},
                "modeled_settlement_pnl": "1",
                "venue_fee_adjusted_pnl": ".9",
            },
        ]
    )
    report = scorecard(rows)["overall"]
    assert report["fills"] == 3 and report["settled_fills"] == 2
    assert report["paper_net_pnl"] == -2 and report["realized_drawdown_by_round"] == 3
    assert report["venue_fee_adjusted_fills"] == 1
    with pytest.raises(ValueError, match="Conflicting"):
        scorecard([*rows, {**rows[-1], "modeled_settlement_pnl": "10"}])


def test_diagnostics_cannot_inflate_operational_coverage():
    report = scorecard(
        [
            {"kind": "venue_reference_close", "start": 0, "matches": True, "diagnostic": True},
            {
                "kind": "market_replay",
                "start": 300,
                "result": {"status": "modeled"},
                "diagnostic": True,
            },
        ]
    )
    assert report["overall"]["windows"] == 0
    assert report["diagnostic_records"] == {"venue_reference_close": 1, "market_replay": 1}
