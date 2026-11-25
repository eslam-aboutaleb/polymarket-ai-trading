"""Hand-built event tapes for the event-driven replay tests. Synthetic by design."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.backtesting.contracts import (
    BookLevel,
    CancelIntent,
    EngineConfig,
    OrderIntent,
    ReplayEvent,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TOKEN = "tok-yes"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def levels(*pairs):
    return [BookLevel(price=Decimal(p), shares=None if s is None else Decimal(s)) for p, s in pairs]


def book(eid, seconds, bids, asks, *, seq=0, token=TOKEN, lag=0.0):
    return ReplayEvent(
        event_id=eid,
        kind="BOOK",
        event_time=at(seconds - lag),
        available_at=at(seconds),
        sequence=seq,
        token_id=token,
        bids=levels(*bids),
        asks=levels(*asks),
    )


def trade(eid, seconds, price, shares, taker, *, seq=0, token=TOKEN):
    return ReplayEvent(
        event_id=eid,
        kind="TRADE",
        event_time=at(seconds),
        available_at=at(seconds),
        sequence=seq,
        token_id=token,
        price=Decimal(price),
        shares=Decimal(shares),
        taker_side=taker,
    )


def signal(
    eid,
    seconds,
    side="BUY",
    amount="50",
    units="USDC",
    *,
    token=TOKEN,
    group=None,
    source_pnl=None,
    limit=None,
    lag=0.0,
):
    return ReplayEvent(
        event_id=eid,
        kind="SIGNAL",
        event_time=at(seconds - lag),
        available_at=at(seconds),
        token_id=token,
        group_id=group,
        side=side,
        amount=Decimal(amount),
        units=units,
        source_pnl=None if source_pnl is None else Decimal(source_pnl),
        limit_price=None if limit is None else Decimal(limit),
    )


def settlement(eid, seconds, payout, *, status="FINAL", confirmed=True, token=TOKEN):
    return ReplayEvent(
        event_id=eid,
        kind="SETTLEMENT",
        event_time=at(seconds),
        available_at=at(seconds),
        token_id=token,
        status=status,
        payout=None if payout is None else Decimal(payout),
        confirmed=confirmed,
    )


def config(**kw):
    base = dict(  # noqa: C408
        funding=Decimal(1000),
        taker_fee_bps=Decimal(100),
        fee_schedule_id="fee-v1",
        maker_fee_bps=Decimal(0),
        seed=7,
    )
    base.update(kw)
    return EngineConfig(**base)


def follow(context):
    """Copy every visible signal at the follower's own size."""
    if context.event.kind != "SIGNAL":
        return []
    e = context.event
    return [
        OrderIntent(
            intent_id=f"o-{e.event_id}",
            token_id=e.token_id,
            side=e.side,
            amount=e.amount,
            units=e.units,
            limit_price=e.limit_price,
        )
    ]


def follow_with_cancel(after_ms):
    def decide(context):
        out = follow(context)
        if out:
            out[0] = out[0].model_copy(update={"order_type": "GTC", "limit_price": Decimal("0.40")})
            out.append(CancelIntent(intent_id=out[0].intent_id, cancel_after_ms=after_ms))
        return out

    return decide
