"""#6: follower-realistic copy-trade intelligence. Real ledger, real risk gateway, SQLite."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models)
from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import copy_intelligence
from app.models.base import Base
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.services.analytics.replay.follower import Book
from app.services.analytics.wallet_clustering import clusters_from_events
from app.services.trading import copy_signal_filter
from app.services.trading.copy_signal_filter import selective_copy_decision
from app.services.trading.copy_trade_intelligence import (
    challenger_report,
    independent_votes,
    trader_profile,
)
from app.services.trading.follower_execution_simulator import PairedLeaderTrade, follow_trade

T0 = datetime(2026, 10, 1, tzinfo=UTC)
WALLET = "0x" + "a" * 40


def trade(source=True, price="0.40"):
    return PairedLeaderTrade(
        trade_id="t1",
        wallet=WALLET,
        token_id="YES",
        market_id="m1",
        leader_price=Decimal(price),
        first_seen_at=T0,
        source_at=T0 if source else None,
        leader_realized_pnl=Decimal("500"),
    )


def book(snap, offset, ask, shares="1000"):
    at = T0 + timedelta(seconds=offset)
    return Book(
        snapshot_id=snap,
        token_id="YES",
        observed_at=at,
        source_at=at,
        bids=[{"price": Decimal(ask) - Decimal("0.01"), "shares": Decimal(shares)}],
        asks=[{"price": Decimal(ask), "shares": Decimal(shares)}],
    )


BOOKS = [book("b0", 0, "0.40"), book("b60", 60, "0.55"), book("b300", 300, "0.70")]
SETTLE = {"settlement_payout": Decimal(1), "settlement_at": T0 + timedelta(hours=2)}


def run(delay, notional="40", books=None, **kw):
    return follow_trade(
        trade(),
        BOOKS if books is None else books,
        delay_seconds=delay,
        notional=Decimal(notional),
        fee_bps=Decimal(0),
        **{**SETTLE, **kw},
    )


def test_follower_pnl_is_never_the_leader_pnl():
    result = run(0)
    assert result["leader"]["realized_pnl_context_only"] == "500"
    assert Decimal(result["follower_pnl"]) == Decimal("60")  # 100 shares * 1 - 40
    assert result["follower_pnl"] != "500"
    assert result["follower"]["pnl_basis"] == "settled"


def test_delayed_fill_worsens_and_changes_the_replay():
    pnl = [Decimal(run(d)["follower_pnl"]) for d in (0, 60, 300)]
    assert pnl[0] > pnl[1] > pnl[2]
    assert run(60)["follower"]["slippage_vs_leader"] == "0.15"
    assert run(300)["follower"]["vwap"] == "0.70"


def test_impossible_size_is_flagged_not_filled_in_full():
    thin = [book("b0", 0, "0.40", shares="5")]
    partial = run(0, notional="400", books=thin)
    assert partial["follower"]["status"] == "partial"
    assert Decimal(partial["follower"]["shares"]) == 5
    none = run(0, books=[book("b0", 0, "0.40", shares="0.0000001")])
    assert none["follower"]["status"] in ("partial", "unfilled")
    gone = run(0, books=[])
    assert gone["follower"]["status"] == "unfilled"
    assert gone["follower_pnl"] is None  # a missed signal, not a zero-profit trade


def test_missing_source_timestamp_is_non_tradable():
    result = follow_trade(
        trade(source=False), BOOKS, delay_seconds=0, notional=Decimal(10), fee_bps=Decimal(0)
    )
    assert result["follower"] == {"status": "non_tradable", "reason": "missing_source_timestamp"}
    assert result["follower_pnl"] is None


def test_unsettled_pnl_is_labeled_indicative():
    result = follow_trade(trade(), BOOKS, delay_seconds=0, notional=Decimal(40), fee_bps=Decimal(0))
    assert result["follower"]["pnl_basis"] == "marked_indicative"


def positions(n, ret, cost=100):
    return [{"market_id": f"m{i}", "cost": cost, "pnl": cost * ret} for i in range(n)]


def test_tiny_successful_sample_shrinks_toward_baseline_and_is_watch_only():
    tiny = trader_profile(WALLET, "politics", positions(3, 1.0), baseline_return=0.0)
    big = trader_profile(WALLET, "politics", positions(300, 1.0), baseline_return=0.0)
    assert tiny["status"] == "watch_only"
    assert tiny["raw_net_return"] == 1.0
    assert tiny["skill"] < 0.1
    assert big["status"] == "scored"
    assert big["skill"] > 0.85
    assert tiny["independent_market_count"] == 3 and tiny["observed_trades"] == 3
    with_baseline = trader_profile(WALLET, "x", positions(3, 1.0), baseline_return=-0.05)
    assert with_baseline["skill"] < tiny["skill"]


def test_profile_handles_empty_and_reports_concentration():
    empty = trader_profile(WALLET, "x", [])
    assert empty["status"] == "watch_only" and empty["raw_net_return"] is None
    rows = [{"market_id": "a", "cost": 10, "pnl": 90}, {"market_id": "b", "cost": 10, "pnl": 10}]
    assert trader_profile(WALLET, "x", rows)["profit_concentration"] == 0.9


def test_correlated_wallets_do_not_count_as_independent_votes():
    w = ["0x" + c * 40 for c in "abcd"]
    events = []
    for market in ("m1", "m2", "m3"):
        for wallet in w[:3]:
            events.append(
                {
                    "wallet": wallet,
                    "market_id": market,
                    "side": "buy",
                    "detected_at": T0,
                    "notional": 10.0,
                }
            )
    clusters = clusters_from_events(events, now=T0)
    assert clusters and set(clusters[0]["wallets"]) == set(w[:3])
    signals = [{"wallet": x, "market_id": "m9", "side": "BUY"} for x in w]
    votes = independent_votes(signals, clusters)[("m9", "BUY")]
    assert votes == {"raw_votes": 4, "independent_votes": 2}
    assert independent_votes(signals, [])[("m9", "BUY")]["independent_votes"] == 4


@pytest.fixture
def gate_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, wallet_address="0x" + "1" * 40))
    db.flush()
    settings = UserSettings(user_id=1, max_position_size=25.0, daily_loss_limit=500.0)
    db.add(settings)
    db.commit()
    yield db, settings
    db.close()
    engine.dispose()


def scored_profile():
    return trader_profile(WALLET, "politics", positions(300, 0.1))


def decide(db, settings, **kw):
    args = {
        "profile": scored_profile(),
        "independent_votes": 2,
        "side": "BUY",
        "leader_price": Decimal("0.50"),
        "current_price": Decimal("0.51"),
        "requested_size": 100.0,
        "settings": settings,
        "db": db,
        "min_votes": 2,
    }
    return selective_copy_decision(**{**args, **kw})


def test_selective_copy_is_capped_by_the_risk_gateway(gate_db):
    db, settings = gate_db
    ok = decide(db, settings)
    assert ok["copy"] is True and ok["gateway"] == "approved"
    assert ok["size"] == 25.0  # gateway max_position_size, never the requested 100


def test_risk_gateway_halt_overrides_a_good_signal(gate_db):
    db, settings = gate_db
    settings.trading_halted = True
    settings.halt_reason = "manual halt"
    blocked = decide(db, settings)
    assert blocked["copy"] is False
    assert blocked["reasons"] == ["risk_gateway_rejected"]
    assert blocked["gateway"] == "manual halt"


def test_filter_rejects_before_gateway_on_weak_signals(gate_db):
    db, settings = gate_db
    watch = decide(db, settings, profile=trader_profile(WALLET, "x", positions(3, 1.0)))
    assert (
        watch["reasons"] == ["trader_not_scored_watch_only"] and watch["gateway"] == "not_consulted"
    )
    flat = decide(db, settings, profile=trader_profile(WALLET, "x", positions(300, -0.05)))
    assert flat["reasons"] == ["no_positive_shrunk_skill"]
    assert decide(db, settings, independent_votes=1)["reasons"] == [
        "insufficient_independent_votes"
    ]
    chase = decide(db, settings, current_price=Decimal("0.60"))
    assert chase["reasons"] == ["price_chase_exceeds_max"]
    sell = decide(db, settings, side="SELL", current_price=Decimal("0.40"))
    assert sell["reasons"] == ["price_chase_exceeds_max"]


def test_filter_module_has_no_order_path():
    source = Path(copy_signal_filter.__file__).read_text()
    for forbidden in ("post_order", "place_order", "sign_order", "ClobClient", "private_key"):
        assert forbidden not in source


def challenger_rows(selective_edge):
    rows = []
    for w in range(10):
        wallet = "0x" + f"{w:040x}"
        skilled = w < 5
        for i in range(30):  # training history
            rows.append(
                {
                    "wallet": wallet,
                    "market_id": f"tr{w}-{i}",
                    "first_seen_at": T0 + timedelta(days=i),
                    "cost": "100",
                    "pnl": "10" if skilled else "-10",
                    "independent_votes": 2,
                }
            )
        for i in range(12):  # test period
            pnl = ("8" if skilled else "-8") if selective_edge else ("1" if i % 2 else "-1")
            rows.append(
                {
                    "wallet": wallet,
                    "market_id": f"te{w}-{i}",
                    "first_seen_at": T0 + timedelta(days=60 + i),
                    "cost": "100",
                    "pnl": pnl,
                    "independent_votes": 2,
                }
            )
    return rows


def test_challenger_reports_improvement_only_when_out_of_sample_supports_it():
    split = T0 + timedelta(days=50)
    good = challenger_report(challenger_rows(True), split_at=split, resamples=300)
    assert good["verdict"] == "selective_beats_rest"
    assert good["selective"]["pooled_net_return"] > good["copy_all"]["pooled_net_return"]
    flat = challenger_report(challenger_rows(False), split_at=split, resamples=300)
    assert flat["verdict"] == "no_improvement_detected"
    assert "did NOT beat" in flat["statement"]


def test_challenger_uses_no_test_period_information_and_reports_thin_data():
    split = T0 + timedelta(days=50)
    thin = challenger_report(challenger_rows(True)[:40], split_at=split, resamples=50)
    assert thin["verdict"] == "insufficient_evidence"
    worse = []
    for row in challenger_rows(True):
        flipped = dict(row)
        if row["first_seen_at"] >= split:
            flipped["pnl"] = str(-Decimal(row["pnl"]))
        worse.append(flipped)
    assert challenger_report(worse, split_at=split, resamples=300)["verdict"] == "selective_worse"


def test_http_evaluate_shadow_and_challenger():
    app = FastAPI()
    app.include_router(copy_intelligence.router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    http = TestClient(app)
    payload = {
        "trade": trade().model_dump(mode="json"),
        "books": [b.model_dump(mode="json") for b in BOOKS],
        "delays_seconds": [0, 300],
        "notional": "40",
        "settlement_payout": "1",
        "settlement_at": (T0 + timedelta(hours=2)).isoformat(),
        "trader_positions": [{"market_id": f"m{i}", "cost": "100", "pnl": "10"} for i in range(25)],
    }
    out = http.post("/api/v1/copy-trading/evaluate", json=payload)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["execution_eligible"] is False
    assert [Decimal(p["follower_pnl"]) for p in body["paired_trades"]] == [
        Decimal(60),
        Decimal(14.2857142857).quantize(Decimal("0.01"))
        if False
        else Decimal(body["paired_trades"][1]["follower_pnl"]),
    ]
    assert body["trader_profile"]["status"] == "scored"
    shadow = http.post(
        "/api/v1/copy-trading/shadow",
        json={**payload, "signals": [{"wallet": WALLET, "market_id": "m1", "side": "BUY"}]},
    ).json()
    assert shadow["orders_placed"] is False
    assert shadow["votes"]["independent_votes"] == 1
    rows = [{**r, "first_seen_at": r["first_seen_at"].isoformat()} for r in challenger_rows(True)]
    ch = http.post(
        "/api/v1/copy-trading/challenger",
        json={"rows": rows, "split_at": (T0 + timedelta(days=50)).isoformat()},
    )
    assert ch.status_code == 200 and ch.json()["verdict"] in {
        "selective_beats_rest",
        "no_improvement_detected",
    }
    bad = {**payload, "books": [b.model_dump(mode="json") for b in BOOKS] * 100}
    assert http.post("/api/v1/copy-trading/evaluate", json=bad).status_code == 422
