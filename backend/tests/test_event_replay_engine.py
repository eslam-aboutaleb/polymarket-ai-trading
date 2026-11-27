import subprocess
import sys
from decimal import Decimal

import pytest
from event_replay_fixtures import (
    TOKEN,
    book,
    config,
    follow,
    follow_with_cancel,
    settlement,
    signal,
    trade,
)
from pydantic import ValidationError

from app.services.backtesting.contracts import EngineConfig, OrderIntent
from app.services.backtesting.event_replay_engine import replay


def run(events, decider=follow, cfg=None, **kw):
    return replay(events, decider, cfg or config(), strategy_version="t-v1", **kw)


FULL = {"bids": [("0.48", 500)], "asks": [("0.50", 500), ("0.52", 500)]}


def _rest_on_signal(ctx):
    if ctx.event.kind != "SIGNAL":
        return []
    return [
        OrderIntent(
            intent_id="m1",
            token_id=TOKEN,
            side="BUY",
            amount=Decimal(10),
            limit_price=Decimal("0.48"),
            order_type="GTC",
        )
    ]


def _same_intent_id(ctx):
    if ctx.event.kind != "SIGNAL":
        return []
    return [
        OrderIntent(
            intent_id="same", token_id=TOKEN, side="BUY", amount=Decimal(10), units="SHARES"
        )
    ]


def tape():
    return [
        book("b1", 0, FULL["bids"], FULL["asks"]),
        signal("s1", 1),
        book("b2", 2, [("0.58", 500)], [("0.60", 500)]),
        signal("s2", 3),
        settlement("final", 10, "1"),
    ]


def test_same_snapshot_and_seed_give_identical_trades():
    cfg = config(decision_latency_ms=100, latency_jitter_ms=900)
    a, b = run(tape(), cfg=cfg), run(tape(), cfg=cfg)
    assert a["fills"] == b["fills"] and a["orders"] == b["orders"]
    assert a["result_hash"] == b["result_hash"]
    assert a["run"]["data_snapshot_id"] == b["run"]["data_snapshot_id"]
    assert a["run"]["seed"] == 7


def test_different_seed_changes_jittered_arrival_but_is_still_reproducible():
    arrivals = {
        tuple(
            o["arrives_at"]
            for o in run(tape(), cfg=config(seed=s, latency_jitter_ms=5000))["orders"]
        )
        for s in range(8)
    }
    assert len(arrivals) > 1


def test_no_look_ahead_late_delivered_book_is_invisible_at_decision_time():
    # The cheap book happened at t=0.5 but our feed only delivered it at t=5.
    late = book("late", 5, [("0.18", 500)], [("0.20", 500)], lag=4.5)
    events = [book("b1", 0, FULL["bids"], FULL["asks"]), late, signal("s1", 1)]
    report = run(events)
    assert report["fills"][0]["price"] == "0.50"
    assert report["orders"][0]["book_event_id"] == "b1"


def test_future_events_never_change_earlier_decisions():
    base = [book("b1", 0, FULL["bids"], FULL["asks"]), signal("s1", 1)]
    extended = [*base, book("b9", 50, [("0.10", 9)], [("0.11", 9)]), signal("s9", 60)]
    a, b = run(base), run(extended)
    assert a["orders"][0] == b["orders"][0] and a["fills"][0] == b["fills"][0]


def test_decider_only_sees_events_available_so_far():
    seen = []

    def spy(context):
        seen.append((context.now, [e.event_id for e in context.visible_events]))
        return []

    run(
        [
            book("b1", 0, FULL["bids"], FULL["asks"]),
            signal("s1", 1),
            book("b2", 5, FULL["bids"], FULL["asks"], lag=4.0),
        ],
        spy,
    )
    assert seen[0][1] == ["b1"] and seen[1][1] == ["b1", "s1"]
    assert "b2" not in seen[1][1]


def test_cash_and_positions_conserve_value_after_fees():
    report = run(tape())
    assert report["conservation"]["holds"] is True
    assert Decimal(report["conservation"]["cash_residual"]) == 0
    pnl = report["pnl"]
    assert Decimal(pnl["net_pnl"]) == Decimal(report["ledger"]["nav"]) - Decimal(1000)
    assert Decimal(pnl["fees"]) > 0
    assert Decimal(pnl["gross_pnl"]) - Decimal(pnl["fees"]) == Decimal(pnl["net_pnl"])
    # YES bought at 0.50 and 0.60 (0.60 ask after the move) and paid out 1.
    assert report["settlements"][TOKEN]["payout"] == "1"
    assert report["ledger"]["positions"] == {}


def test_delaying_the_copy_changes_fills_and_pnl():
    events = [
        book("b1", 0, FULL["bids"], FULL["asks"]),
        signal("s1", 1),
        book("b2", 2, [("0.58", 500)], [("0.60", 500)]),
        settlement("f", 20, "1"),
    ]
    fast = run(events, cfg=config(decision_latency_ms=0))
    slow = run(events, cfg=config(decision_latency_ms=1500))
    assert fast["fills"][0]["price"] == "0.50"
    assert slow["fills"][0]["price"] == "0.60"
    assert Decimal(slow["pnl"]["net_pnl"]) < Decimal(fast["pnl"]["net_pnl"])
    assert slow["result_hash"] != fast["result_hash"]


def test_equal_instant_book_move_applies_before_arrival():
    events = [
        book("b1", 0, FULL["bids"], FULL["asks"]),
        signal("s1", 1),
        book("b2", 2, [("0.58", 500)], [("0.60", 500)]),
    ]
    report = run(events, cfg=config(decision_latency_ms=1000))
    assert report["fills"][0]["price"] == "0.60"


def test_partial_book_cannot_produce_a_full_guaranteed_fill():
    thin = [book("b1", 0, [("0.48", 500)], [("0.50", 8)]), signal("s1", 1, amount="50")]
    report = run(thin)
    order = report["orders"][0]
    assert order["status"] == "partial_ioc_cancelled"
    assert Decimal(order["filled_shares"]) == Decimal("8.00")
    assert Decimal(order["filled_shares"]) < Decimal(order["requested_shares"])
    assert Decimal(report["ledger"]["cash"]) > 0


def test_limit_price_blocks_deeper_levels():
    events = [
        book("b1", 0, FULL["bids"], [("0.50", 6), ("0.55", 500)]),
        signal("s1", 1, amount="200", limit="0.52"),
    ]
    report = run(events)
    assert [f["price"] for f in report["fills"]] == ["0.50"]
    assert report["orders"][0]["status"] == "partial_ioc_cancelled"


def test_no_historical_depth_is_labeled_approximation_and_capped():
    events = [book("b1", 0, [("0.48", None)], [("0.50", None)]), signal("s1", 1, amount="500")]
    report = run(events)
    assert report["run"]["quality_status"] == "approximate"
    assert any(u.startswith("approximation_no_depth") for u in report["uncertainty"])
    assert report["fills"][0]["depth_quality"] == "approximation_no_depth"
    assert Decimal(report["fills"][0]["shares"]) <= Decimal(5)


def test_insufficient_collateral_rejects_instead_of_overdrawing():
    events = [book("b1", 0, FULL["bids"], FULL["asks"]), signal("s1", 1, amount="5000")]
    report = run(events, cfg=config(funding=Decimal(20)))
    order = report["orders"][0]
    assert Decimal(report["ledger"]["cash"]) >= 0
    assert Decimal(order["filled_shares"]) * Decimal("0.50") <= Decimal(20)
    broke = run(events, cfg=config(funding=Decimal(1)))
    assert broke["orders"][0]["status"].startswith("rejected")
    assert broke["fills"] == []


def test_unknown_fee_withholds_net_pnl_rather_than_assuming_zero():
    cfg = EngineConfig(funding=Decimal(1000))
    report = run(tape(), cfg=cfg)
    assert report["pnl"]["net_pnl"] is None and report["pnl"]["fees"] is None
    assert report["pnl"]["fee_status"] == "unknown"
    assert report["fills"][0]["fee"] is None
    assert report["run"]["quality_status"] == "approximate"


def test_fee_schedule_and_rate_must_be_supplied_together():
    with pytest.raises(ValidationError):
        EngineConfig(taker_fee_bps=Decimal(100))


def test_source_trader_pnl_is_never_copied():
    a = run(
        [
            book("b1", 0, FULL["bids"], FULL["asks"]),
            signal("s1", 1, source_pnl="999999"),
            settlement("f", 5, "0"),
        ]
    )
    b = run(
        [
            book("b1", 0, FULL["bids"], FULL["asks"]),
            signal("s1", 1, source_pnl="-5"),
            settlement("f", 5, "0"),
        ]
    )
    assert a["pnl"]["net_pnl"] == b["pnl"]["net_pnl"]
    assert Decimal(a["pnl"]["net_pnl"]) < 0  # token paid zero; follower really lost money


def test_maker_fill_needs_observed_order_flow_and_is_otherwise_uncertain():
    quiet = run([book("b1", 0, [("0.48", 100)], [("0.50", 100)]), signal("s1", 1)], _rest_on_signal)
    assert quiet["fills"] == [] and quiet["orders"][0]["status"] == "resting"
    # A trade AT our price with unknown queue is uncertain, not a fill.
    unknown = run(
        [
            book("b1", 0, [("0.48", None)], [("0.50", None)]),
            signal("s1", 1),
            trade("t1", 2, "0.48", 50, "SELL"),
        ],
        _rest_on_signal,
    )
    assert unknown["fills"] == [] and unknown["uncertain_maker_fills"]
    assert unknown["run"]["quality_status"] == "approximate"
    # Observed queue of 100 ahead: 50 traded is not enough, 120 more fills us.
    q1 = run(
        [
            book("b1", 0, [("0.48", 100)], [("0.50", 100)]),
            signal("s1", 1),
            trade("t1", 2, "0.48", 50, "SELL"),
        ],
        _rest_on_signal,
    )
    assert q1["fills"] == []
    q2 = run(
        [
            book("b1", 0, [("0.48", 100)], [("0.50", 100)]),
            signal("s1", 1),
            trade("t1", 2, "0.48", 50, "SELL"),
            trade("t2", 3, "0.48", 70, "SELL"),
        ],
        _rest_on_signal,
    )
    assert [Decimal(f["shares"]) for f in q2["fills"]] == [Decimal(10)] and q2["fills"][0][
        "liquidity"
    ] == "maker"
    # A trade through our price proves the queue cleared.
    through = run(
        [
            book("b1", 0, [("0.48", 100)], [("0.50", 100)]),
            signal("s1", 1),
            trade("t1", 2, "0.47", 4, "SELL"),
        ],
        _rest_on_signal,
    )
    assert [Decimal(f["shares"]) for f in through["fills"]] == [Decimal(4)]
    assert through["fills"][0]["price"] == "0.48"  # filled at our limit, never better
    assert through["orders"][0]["status"] == "resting_partial"


def test_cancel_fill_race_fill_wins_when_it_happens_first():
    events = [
        book("b1", 0, [("0.40", 100)], [("0.50", 100)]),
        signal("s1", 1, amount="10", units="SHARES"),
        trade("t1", 2, "0.39", 500, "SELL"),
    ]
    late_cancel = run(events, follow_with_cancel(3000), config(minimum_shares=Decimal(1)))
    assert late_cancel["orders"][0]["status"] == "filled"
    assert late_cancel["rejected_cancels"][0]["result"] == "rejected_too_late"
    early_cancel = run(events, follow_with_cancel(500), config(minimum_shares=Decimal(1)))
    assert early_cancel["orders"][0]["status"] == "cancelled"
    assert early_cancel["fills"] == []


def test_cancel_before_arrival_wins_the_race():
    report = run(
        [
            book("b1", 0, [("0.40", 100)], [("0.50", 100)]),
            signal("s1", 1, amount="10", units="SHARES"),
        ],
        follow_with_cancel(0),
        config(decision_latency_ms=2000, minimum_shares=Decimal(1)),
    )
    assert report["orders"][0]["status"] == "cancelled_before_arrival"
    assert report["fills"] == []


def test_market_resolving_while_order_pending_rejects_the_order():
    events = [book("b1", 0, FULL["bids"], FULL["asks"]), signal("s1", 1), settlement("f", 2, "1")]
    report = run(events, cfg=config(decision_latency_ms=3000))
    assert report["orders"][0]["status"] == "rejected_market_settled"
    assert report["fills"] == []


def test_unconfirmed_and_disputed_settlement_is_never_credited():
    events = [
        *tape()[:2],
        settlement("prov", 5, "1", confirmed=False),
        settlement("disp", 6, None, status="DISPUTED"),
    ]
    report = run(events)
    assert report["settlements"] == {}
    assert report["ledger"]["positions"]
    assert any("unconfirmed" in u for u in report["uncertainty"])
    assert any("disputed" in u for u in report["uncertainty"])


def test_void_settlement_pays_the_declared_refund_rate():
    report = run(
        [
            book("b1", 0, FULL["bids"], FULL["asks"]),
            signal("s1", 1),
            settlement("void", 5, "0.5", status="VOID"),
        ]
    )
    assert report["settlements"][TOKEN]["status"] == "VOID"
    assert report["conservation"]["holds"] is True
    assert report["ledger"]["positions"] == {}


def test_gtc_pending_order_is_cancelled_by_settlement():
    report = run(
        [book("b1", 0, [("0.48", 100)], [("0.50", 100)]), signal("s1", 1), settlement("f", 5, "0")],
        _rest_on_signal,
    )
    assert report["orders"][0]["status"].startswith("cancelled")
    assert report["orders"][0]["reason"] == "cancelled_by_settlement"
    assert report["conservation"]["holds"] is True


def test_stale_book_is_rejected():
    report = run([book("b1", 0, FULL["bids"], FULL["asks"]), signal("s1", 5000)])
    assert report["orders"][0]["status"] == "rejected_no_fresh_book"


def test_tick_rounding_never_makes_limit_more_aggressive():
    events = [
        book("b1", 0, FULL["bids"], [("0.50", 100), ("0.51", 100)]),
        signal("s1", 1, amount="500", limit="0.517"),
    ]
    report = run(events)
    assert report["orders"][0]["limit_price"] == "0.51"


def test_event_contract_rejects_future_source_clock_and_crossed_books():
    with pytest.raises(ValidationError):
        book("x", 0, [("0.5", 1)], [("0.4", 1)])
    with pytest.raises(ValidationError):
        book("x", 0, [("0.4", 1)], [("0.5", 1)], lag=-1.0)


def test_conflicting_duplicate_event_ids_fail_loudly():
    a, b = book("b1", 0, FULL["bids"], FULL["asks"]), book("b1", 0, [("0.47", 1)], [("0.50", 1)])
    with pytest.raises(ValueError, match="conflicting"):
        run([a, b])
    assert run([a, a])["run"]["event_count"] == 1


def test_duplicate_intent_ids_fail_loudly():
    with pytest.raises(ValueError, match="duplicate intent"):
        run(
            [book("b1", 0, FULL["bids"], FULL["asks"]), signal("s1", 1), signal("s2", 2)],
            _same_intent_id,
        )


def test_replay_package_has_no_signing_or_order_gateway_dependency():
    code = (
        "import sys, app.services.backtesting.event_replay_engine, "
        "app.services.backtesting.walk_forward_validator, app.services.backtesting.strategy_lab, "
        "app.services.backtesting.shadow_runner;"
        "roots=('py_clob_client','eth_account','web3','httpx','sqlalchemy');"
        "bad=[m for m in sys.modules if m.split('.')[0] in roots "
        "or m.startswith('app.services.trading')];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
