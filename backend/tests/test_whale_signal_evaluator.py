"""#11: causal whale/inverse evaluation. Real evaluator, real clustering, SQLite for routes."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets import whale_signal_quality
from app.models.base import Base
from app.models.market_data.replay_capture import ReplayCapture
from app.models.market_data.whale_event import WhaleEvent
from app.services.analytics.wallet_clustering import clusters_from_events
from app.services.intelligence.whale_signal_evaluator import (
    Quote,
    Settlement,
    WhaleSignal,
    dedupe_signals,
    evaluate,
    signal_outcome,
)
from app.utils.database import get_db

T0 = datetime(2026, 10, 1, tzinfo=UTC)
W = ["0x" + c * 40 for c in "abcdef"]


def sig(i, wallet=None, observed=None, side="BUY", **kw):
    return WhaleSignal(
        event_id=f"e{i}-{wallet or 0}",
        wallet=wallet or W[0],
        market_id=f"m{i}",
        token_id=f"T{i}",
        complement_token_id=f"N{i}",
        side=side,
        observed_at=observed or T0 + timedelta(hours=i),
        **kw,
    )


def quotes_for(signal, jump_after_min=20, pre="0.50", post="0.70", token=None, span=480):
    out = []
    base = signal.observed_at
    for minute in range(-span, span + 1):
        ask = Decimal(pre) if minute < jump_after_min else Decimal(post)
        out.append(
            Quote(
                token_id=token or signal.token_id,
                observed_at=base + timedelta(minutes=minute),
                bid=ask - Decimal("0.01"),
                ask=ask,
            )
        )
    return out


def settle(signal, payout="1", token=None):
    return Settlement(
        token_id=token or signal.token_id,
        observed_at=signal.observed_at + timedelta(days=2),
        payout=Decimal(payout),
    )


def test_entry_never_uses_a_price_from_before_observation():
    s = sig(1)
    quotes = [
        Quote(token_id="T1", observed_at=s.observed_at - timedelta(minutes=5), ask=Decimal("0.10")),
        Quote(
            token_id="T1", observed_at=s.observed_at + timedelta(seconds=60), ask=Decimal("0.60")
        ),
    ]
    out = signal_outcome(s, quotes, [settle(s)], delay_seconds=60)
    assert out["status"] == "ok" and out["cost"] == Decimal("0.60")
    assert (
        signal_outcome(s, quotes[:1], [settle(s)], delay_seconds=60)["status"] == "no_entry_quote"
    )


def test_late_backfill_enters_at_receipt_time_not_event_time():
    s = sig(1, event_time=T0 - timedelta(hours=3))
    early = Quote(token_id="T1", observed_at=T0 - timedelta(hours=3), ask=Decimal("0.20"))
    late = Quote(
        token_id="T1", observed_at=s.observed_at + timedelta(seconds=90), ask=Decimal("0.80")
    )
    out = signal_outcome(s, [early, late], [settle(s)], delay_seconds=60)
    assert out["cost"] == Decimal("0.80")


def test_event_time_after_receipt_is_excluded():
    bad = sig(1, event_time=T0 + timedelta(days=5))
    report = evaluate([bad], [], [], [], split_at=T0, resamples=10)
    assert report["excluded"]["event_time_after_receipt"] == 1
    assert report["trade_suggestion"] is None


def test_clustered_wallets_and_duplicates_count_once_and_self_trades_never():
    events = [
        {"wallet": w, "market_id": m, "side": "buy", "detected_at": T0, "notional": 5.0}
        for m in ("m1", "m2", "m3")
        for w in W[:3]
    ]
    clusters = clusters_from_events(events, now=T0)
    signals = [sig(1, w, observed=T0 + timedelta(minutes=k)) for k, w in enumerate(W[:3])]
    signals.append(sig(1, W[0], observed=T0 + timedelta(minutes=1)))  # repeat of one event
    unique, dropped = dedupe_signals(signals, clusters)
    assert len(unique) == 1 and dropped["duplicate"] == 3
    both = [sig(2, W[4], side="BUY"), sig(2, W[4], side="SELL")]
    unique, dropped = dedupe_signals(both, [])
    assert unique == [] and dropped["self_trade"] == 2
    sells, dropped = dedupe_signals([sig(3, side="SELL")], [])
    assert sells == [] and dropped["sell_not_followable"] == 1


def test_inverse_is_measured_on_complement_net_of_fees():
    s = sig(1)
    quotes = quotes_for(s, token="N1", pre="0.40", post="0.40") + quotes_for(s)
    lost = Settlement(
        token_id="N1", observed_at=s.observed_at + timedelta(days=2), payout=Decimal(0)
    )
    inverse = signal_outcome(s, quotes, [lost], delay_seconds=60, inverse=True)
    assert inverse["net_return"] == Decimal(-1)
    cheap = Settlement(
        token_id="N1", observed_at=s.observed_at + timedelta(days=2), payout=Decimal(1)
    )
    free = signal_outcome(s, quotes, [cheap], delay_seconds=60, inverse=True)
    fee = signal_outcome(s, quotes, [cheap], delay_seconds=60, inverse=True, fee_bps=Decimal(500))
    assert fee["net_return"] < free["net_return"]
    no_comp = sig(2).model_copy(update={"complement_token_id": None})
    assert (
        signal_outcome(no_comp, [], [], delay_seconds=60, inverse=True)["status"]
        == "missing_complement_token"
    )


def build(n, payout="1"):
    signals = [sig(i, W[i % 3 + 3]) for i in range(n)]
    quotes = [q for s in signals for q in quotes_for(s)]
    settlements = [settle(s, payout) for s in signals]
    return signals, quotes, settlements


def test_time_split_is_disjoint_by_market_and_reports_sample_size_and_uncertainty():
    signals, quotes, settlements = build(40)
    split = T0 + timedelta(hours=20)
    report = evaluate(
        signals, quotes, settlements, [], split_at=split, resamples=100, placebo_runs=3
    )
    d = report["delays"][0]
    assert d["in_sample"]["distinct_markets"] == 20
    assert d["out_of_sample"]["distinct_markets"] == 20
    assert d["out_of_sample"]["ci95"] is not None
    assert d["out_of_sample"]["signals"] == 20
    # a later signal in an already-seen market belongs to the in-sample period
    extra = sig(0, W[5], observed=T0 + timedelta(hours=30)).model_copy(
        update={"event_id": "late-m0"}
    )
    again = evaluate(
        [*signals, extra], quotes, settlements, [], split_at=split, resamples=50, placebo_runs=2
    )
    assert again["delays"][0]["out_of_sample"]["signals"] == 20


def test_suggestion_only_with_settled_edge_enough_markets_and_placebo_margin():
    signals, quotes, settlements = build(80)
    split = T0 + timedelta(hours=40)
    report = evaluate(
        signals, quotes, settlements, [], split_at=split, resamples=200, placebo_runs=10
    )
    assert report["abstain_reasons"] == []
    assert report["trade_suggestion"]["delay_seconds"] in (60, 300)
    assert report["informational_only"] is False
    assert report == evaluate(
        signals, quotes, settlements, [], split_at=split, resamples=200, placebo_runs=10
    )


def test_abstains_when_outcomes_lose_after_costs_or_sample_is_small_or_unsettled():
    signals, quotes, _ = build(80)
    losers = [settle(s, "0") for s in signals]
    split = T0 + timedelta(hours=40)
    lost = evaluate(signals, quotes, losers, [], split_at=split, resamples=100, placebo_runs=3)
    assert lost["trade_suggestion"] is None
    assert "interval_includes_zero_after_costs" in lost["abstain_reasons"]
    small = evaluate(
        signals[:10],
        quotes,
        [settle(s) for s in signals[:10]],
        [],
        split_at=T0 + timedelta(hours=5),
        resamples=50,
        placebo_runs=2,
    )
    assert "too_few_distinct_markets" in small["abstain_reasons"]
    markout = evaluate(signals, quotes, [], [], split_at=split, resamples=100, placebo_runs=3)
    assert markout["trade_suggestion"] is None
    assert (
        "not_settlement_confirmed" in markout["abstain_reasons"]
        or "missing_evidence" in markout["abstain_reasons"]
    )
    none = evaluate(signals, [], [], [], split_at=split, resamples=10, placebo_runs=1)
    assert none["abstain_reasons"] == ["missing_evidence"] and none["trade_suggestion"] is None


def test_no_edge_signal_is_not_suggested_because_placebo_matches():
    signals = [sig(i, W[3]) for i in range(80)]
    quotes = [q for s in signals for q in quotes_for(s, jump_after_min=-9999, post="0.50")]
    settlements = [settle(s) for s in signals]
    report = evaluate(
        signals,
        quotes,
        settlements,
        [],
        split_at=T0 + timedelta(hours=40),
        resamples=100,
        placebo_runs=10,
    )
    assert report["trade_suggestion"] is None
    assert "not_better_than_placebo" in report["abstain_reasons"]


@pytest.fixture
def http():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[WhaleEvent.__table__, ReplayCapture.__table__])
    factory = sessionmaker(bind=engine)

    def sessions():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(whale_signal_quality.router)
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


def test_routes_abstain_without_evidence_and_never_suggest_without_settlements(http):
    client, factory = http
    empty = client.get(f"/api/v1/intelligence/wallets/{W[0]}/signal-quality").json()
    assert empty["trade_suggestion"] is None and empty["abstain_reasons"] == ["missing_evidence"]
    assert client.get("/api/v1/intelligence/wallets/bad/signal-quality").status_code == 422
    with factory() as db:
        for i in range(6):
            when = T0 + timedelta(hours=i)
            db.add(
                WhaleEvent(
                    wallet=W[0],
                    market_id=f"m{i}",
                    token_id=f"T{i}",
                    side="buy",
                    size=1,
                    price=0.5,
                    notional=500,
                    tx_hash=f"0x{i}",
                    log_index=0,
                    block_ts=when,
                    detected_at=when + timedelta(seconds=5),
                )
            )
            for minute, ask in ((1, "0.5"), (62, "0.6")):
                book = {
                    "book": {
                        "bids": [{"price": str(Decimal(ask) - Decimal("0.01")), "shares": "10"}],
                        "asks": [{"price": ask, "shares": "10"}],
                    }
                }
                db.add(
                    ReplayCapture(
                        id=f"c{i}-{minute}",
                        kind="book",
                        source_key="x",
                        token_id=f"T{i}",
                        observed_at=when + timedelta(minutes=minute),
                        content_hash="h",
                        payload_json=json.dumps(book),
                    )
                )
        db.add(
            WhaleEvent(
                wallet=W[0],
                market_id="x",
                token_id="TX",
                side="transfer",
                size=1,
                price=0,
                notional=1,
                tx_hash="0xx",
                detected_at=T0,
            )
        )
        db.commit()
    body = client.get(f"/api/v1/intelligence/wallets/{W[0]}/signal-quality").json()
    assert body["events_considered"] == 6 and body["skipped_event_types"] == 1
    assert body["trade_suggestion"] is None and body["informational_only"] is True
    assert body["delays"][0]["out_of_sample"]["bases"] in ([], ["markout"])
    inverse = client.get("/api/v1/intelligence/inverse/validation").json()
    assert inverse["inverse"] is True and inverse["trade_suggestion"] is None
    assert inverse["delays"][0]["out_of_sample"]["missing"].get("missing_complement_token")
