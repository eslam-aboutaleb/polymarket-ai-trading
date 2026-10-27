from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.trading.paper_maker_lifecycle import (
    FlowEvent,
    MakerOrder,
    ReplayState,
    replay_flow,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)


def order(**changes):
    return MakerOrder(
        **{
            "id": "o1",
            "token_id": "yes",
            "condition_id": "c1",
            "side": "BUY",
            "limit_price": Decimal("0.45"),
            "shares": Decimal("10"),
            "active_from": NOW,
            "expires_at": NOW + timedelta(minutes=5),
            "source_snapshot_id": "book1",
            "queue_ahead": Decimal("3"),
            "fee_bps": Decimal("100"),
            "fee_version": "flat-test-v1",
            **changes,
        }
    )


def event(**changes):
    return FlowEvent(
        **{
            "id": "e1",
            "token_id": "yes",
            "condition_id": "c1",
            "occurred_at": NOW + timedelta(seconds=1),
            "available_at": NOW + timedelta(seconds=2),
            "price": Decimal("0.45"),
            "shares": Decimal("8"),
            "aggressor": "SELL",
            "continuous": True,
            "evidence_snapshot_id": "flow1",
            **changes,
        }
    )


def run(orders=None, events=None, state=None, cutoff=None):
    return replay_flow(
        state or ReplayState(orders=orders or [order()]),
        events or [],
        cutoff or NOW + timedelta(seconds=10),
    )


def test_static_hundred_replays_do_not_fill_or_replace_identity():
    state = ReplayState(orders=[order()])
    for _ in range(100):
        result = run(state=state)
        assert not result.fills
        state = result.state
    assert state.orders[0].id == "o1"
    assert state.orders[0].state == "RESTING"


def test_queue_consumption_partial_fee_and_restart_dedup():
    result = run(events=[event()])
    assert result.fills[0].shares == Decimal("5")
    assert result.fills[0].fee_usdc == Decimal("0.0225")
    assert result.state.orders[0].state == "PARTIAL"
    restored = ReplayState.model_validate_json(result.state.model_dump_json())
    repeated = run(state=restored, events=[event()])
    assert not repeated.fills
    assert repeated.state.orders[0].cumulative_filled == Decimal("5")
    with pytest.raises(ValueError, match="conflicting"):
        run(state=restored, events=[event(shares=Decimal("9"))])


@pytest.mark.parametrize(
    "change",
    [
        {"aggressor": None},
        {"aggressor": "BUY"},
        {"token_id": "no"},
        {"price": Decimal("0.46")},
        {"occurred_at": NOW - timedelta(seconds=1)},
    ],
)
def test_wrong_side_token_price_or_preeligibility_cannot_fill(change):
    assert not run(events=[event(**change)]).fills


@pytest.mark.parametrize(
    "change", [{"queue_ahead": None}, {"fee_bps": None}, {"fee_version": None}]
)
def test_missing_queue_or_fee_abstains(change):
    assert not run(orders=[order(**change)], events=[event()]).fills


def test_shared_print_budget_cannot_fill_multiple_quotes_twice():
    result = run(
        orders=[order(queue_ahead=Decimal(0)), order(id="o2", queue_ahead=Decimal(0))],
        events=[event(shares=Decimal("15"))],
    )
    assert sum(f.shares for f in result.fills) == Decimal("15")
    assert [o.state for o in result.state.orders] == ["FILLED", "PARTIAL"]


def test_cancel_effective_boundary_and_expiry_win_conservatively():
    assert not run(
        orders=[order(cancel_effective_at=NOW + timedelta(seconds=2))], events=[event()]
    ).fills
    expired = run(orders=[order(expires_at=NOW + timedelta(seconds=2))], events=[event()])
    assert not expired.fills and expired.state.orders[0].state == "EXPIRED"


def test_gap_freezes_until_new_order_and_future_receipts_excluded():
    result = run(events=[event(continuous=False)])
    assert result.state.orders[0].state == "UNKNOWN_DATA"
    assert not run(
        state=result.state, events=[event(id="e2", available_at=NOW + timedelta(seconds=3))]
    ).fills
    assert not run(events=[event()], cutoff=NOW + timedelta(seconds=1)).fills


def test_sell_uses_buy_aggressor_and_order_limit():
    result = run(
        orders=[order(side="SELL", limit_price=Decimal("0.55"))],
        events=[event(aggressor="BUY", price=Decimal("0.56"))],
    )
    assert result.fills[0].price == Decimal("0.55")


@pytest.mark.parametrize("volume", ["0.01", "1", "3", "3.01", "10", "13", "100"])
def test_capacity_invariants(volume):
    amount = Decimal(volume)
    result = run(events=[event(shares=amount)])
    filled = sum((f.shares for f in result.fills), Decimal(0))
    assert filled == min(Decimal(10), max(Decimal(0), amount - Decimal(3)))
    assert filled <= amount
    assert result.state.orders[0].cumulative_filled == filled


def test_reordered_new_event_freezes_and_backdated_replay_rejects():
    first = run(events=[event()])
    older = event(id="late", available_at=NOW + timedelta(seconds=1))
    result = run(state=first.state, events=[older])
    assert not result.fills and result.state.orders[0].state == "UNKNOWN_DATA"
    with pytest.raises(ValueError, match="forward"):
        run(state=first.state, cutoff=NOW)


def test_partial_then_cancel_keeps_quantity_and_no_later_fill():
    first = run(
        orders=[order(cancel_effective_at=NOW + timedelta(seconds=4))],
        events=[event()],
        cutoff=NOW + timedelta(seconds=3),
    )
    later = run(
        state=first.state,
        events=[
            event(
                id="after",
                occurred_at=NOW + timedelta(seconds=5),
                available_at=NOW + timedelta(seconds=6),
            )
        ],
    )
    assert not later.fills
    assert later.state.orders[0].cumulative_filled == Decimal(5)
    assert later.state.orders[0].state == "CANCELLED"
