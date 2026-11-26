from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from trader_eval_fixtures import T0, book, position

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import trader_evaluation
from app.services.analytics.follower_lag import LagSettings, LeaderTrade
from app.services.analytics.trader_scoring import ResolvedPosition
from app.services.analytics.trader_walk_forward import TraderHistory, walk_forward

CUT = T0 + timedelta(days=60)
END = T0 + timedelta(days=120)


def wallet(i):
    return "0x" + f"{i:040x}"


def history(i, skilled, trades=12, test_payout="1"):
    """Train record: skilled traders net positive, unskilled net negative."""
    positions = [position(d, "100", "20" if skilled else "-20", day=d) for d in range(30)]
    leader_trades = []
    for k in range(trades):
        leader_trades.append(
            LeaderTrade(
                trade_id=f"{i}-{k}",
                token_id=f"tok-{i}-{k}",
                side="BUY",
                leader_price=Decimal("0.50"),
                first_seen_at=CUT + timedelta(hours=k + 1),
                payout=Decimal(test_payout if (skilled or k % 3 == 0) else "0"),
            )
        )
    return TraderHistory(wallet=wallet(i), positions=positions, leader_trades=leader_trades)


def books_for(histories):
    rows = []
    for h in histories:
        for t in h.leader_trades:
            rows.append(
                book(f"b-{t.trade_id}", t.token_id, t.first_seen_at + timedelta(seconds=60), "0.52")
            )
    return rows


def run(histories, **kwargs):
    return walk_forward(
        histories,
        books_for(histories),
        cutoffs=[CUT],
        end_at=END,
        delay_seconds=60,
        settings=LagSettings(),
        resamples=300,
        **kwargs,
    )


def test_top_beats_bottom_when_score_predicts_followers():
    histories = [history(i, skilled=i < 6) for i in range(12)]
    report = run(histories)
    assert report["verdict"] == "top_beats_bottom"
    assert report["difference_ci95"][0] > 0
    assert (
        report["top_bucket"]["pooled_net_follower_return"]
        > report["bottom_bucket"]["pooled_net_follower_return"]
    )
    assert report == run(histories)  # deterministic


def test_reports_no_advantage_when_test_returns_are_random():
    histories = [history(i, skilled=i < 6, test_payout="1") for i in range(12)]
    # make every trader's out-of-sample outcomes identical regardless of score
    flat = []
    for h in histories:
        trades = [
            t.model_copy(update={"payout": Decimal("1" if n % 2 == 0 else "0")})
            for n, t in enumerate(h.leader_trades)
        ]
        flat.append(h.model_copy(update={"leader_trades": trades}))
    report = run(flat)
    assert report["verdict"] == "no_difference_detected"
    assert "did NOT beat" in report["statement"]


def test_scores_use_only_positions_closed_before_cutoff():
    """A trader who only looks great AFTER the cutoff must stay unscored in training."""
    late = TraderHistory(
        wallet=wallet(99),
        positions=[position(d, "100", "50", day=70 + d % 40) for d in range(40)],
        leader_trades=[],
    )
    histories = [history(i, skilled=i < 6) for i in range(12)] + [late]
    report = run(histories)
    assert wallet(99) not in report["folds"][0]["top"]
    assert wallet(99) not in report["folds"][0]["bottom"]
    assert report["folds"][0]["scored_traders"] == 12


def test_insufficient_evidence_with_few_traders_or_trades():
    few = [history(i, skilled=i < 2, trades=1) for i in range(4)]
    report = run(few)
    assert report["verdict"] == "insufficient_evidence"
    assert "No advantage is claimed" in report["statement"]


def test_unfilled_follower_trades_are_missing_not_zero():
    histories = [history(i, skilled=i < 6) for i in range(12)]
    report = walk_forward(
        histories, [], cutoffs=[CUT], end_at=END, settings=LagSettings(), resamples=100
    )
    assert report["top_bucket"]["settled_trades"] == 0
    assert report["verdict"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cutoffs": []},
        {"cutoffs": [CUT, CUT]},
        {"cutoffs": [END], "end_at": END},
        {"quantile": 0.9},
    ],
)
def test_invalid_arguments_rejected(kwargs):
    base = {"cutoffs": [CUT], "end_at": END}
    with pytest.raises(ValueError):
        walk_forward([], [], **{**base, **kwargs})


def test_http_endpoints_roundtrip():
    app = FastAPI()
    app.include_router(trader_evaluation.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    http = TestClient(app)
    rows = [
        ResolvedPosition(
            market_id=f"m{i}", cost=Decimal(100), pnl=Decimal(8), closed_at=T0 + timedelta(days=i)
        ).model_dump(mode="json")
        for i in range(30)
    ]
    scored = http.post("/api/trades/traders/score", json={"positions": rows})
    assert scored.status_code == 200 and scored.json()["status"] == "scored"
    skip = http.post(
        "/api/trades/traders/copy-skip",
        json={"side": "BUY", "leader_price": "0.5", "current_price": "0.6", "edge": "0.05"},
    )
    assert skip.status_code == 200
    assert skip.json()["skip"] is True and skip.json()["max_edge_fraction"] == "0.5"
    custom = http.post(
        "/api/trades/traders/copy-skip",
        json={
            "side": "BUY",
            "leader_price": "0.5",
            "current_price": "0.51",
            "edge": "0.05",
            "max_edge_fraction": "0.1",
        },
    )
    assert custom.json()["skip"] is True
    lag = http.post(
        "/api/trades/traders/follower-lag",
        json={
            "trades": [
                {
                    "trade_id": "a",
                    "token_id": "YES",
                    "side": "BUY",
                    "leader_price": "0.4",
                    "first_seen_at": T0.isoformat(),
                    "payout": "1",
                }
            ],
            "books": [book("b", "YES", T0, "0.40").model_dump(mode="json")],
            "delays_seconds": [0],
        },
    )
    assert lag.status_code == 200
    assert lag.json()["scenarios"][0]["filled"] == 1
    bad = http.post(
        "/api/trades/traders/walk-forward",
        json={
            "histories": [h.model_dump(mode="json") for h in [history(1, True), history(2, False)]],
            "books": [],
            "cutoffs": [END.isoformat()],
            "end_at": CUT.isoformat(),
        },
    )
    assert bad.status_code == 422
    ok = http.post(
        "/api/trades/traders/walk-forward",
        json={
            "histories": [h.model_dump(mode="json") for h in [history(1, True), history(2, False)]],
            "books": [],
            "cutoffs": [CUT.isoformat()],
            "end_at": END.isoformat(),
        },
    )
    assert ok.status_code == 200 and ok.json()["verdict"] == "insufficient_evidence"
    assert http.post("/api/trades/traders/score", json={"positions": [], "x": 1}).status_code == 422
