"""Captured native object fixtures, with source clocks independent from receipt clocks."""

from datetime import UTC, datetime
from decimal import Decimal

from latency_book_fixtures import snapshot
from paper_account_fixtures import CID, RULE

from app.services.analytics.replay.follower import Book, Level
from app.services.market_data.replay_capture import append_capture


def native(db, clock, *, ask="0.50", bid="0.49", depth="100", source_at=None, final=None):
    moment = clock[0]
    source = source_at or moment
    source = datetime.fromtimestamp(int(source.timestamp() * 1000) / 1000, UTC)
    raw, metadata, _ = snapshot(source)
    raw.update(
        market=CID,
        tick_size="0.01",
        min_order_size="0.01",
        bids=[{"price": bid, "size": depth}],
        asks=[{"price": ask, "size": depth}],
    )
    clob, gamma = metadata["raw"]["clob"], metadata["raw"]["gamma"]
    clob.update(condition_id=CID, minimum_tick_size="0.01", minimum_order_size="0.01")
    gamma["conditionId"] = CID
    if final:
        clob.update(active=False, closed=True, accepting_orders=False)
        gamma.update(active=False, closed=True, umaResolutionStatus="resolved")
    for kind, value in (("clob_market", clob), ("gamma", gamma)):
        append_capture(
            db,
            kind=kind,
            source_key=CID,
            raw=value,
            observed_at=moment,
            condition_id=CID,
            rule_hash=RULE,
        )
    book = Book(
        snapshot_id=str(moment.timestamp()).replace(".", "-"),
        token_id="11",
        observed_at=moment,
        source_at=source,
        bids=[Level(price=Decimal(bid), shares=Decimal(depth))],
        asks=[Level(price=Decimal(ask), shares=Decimal(depth))],
    )
    return append_capture(
        db,
        kind="book",
        source_key=book.snapshot_id,
        raw={"book": book.model_dump(mode="json"), "venue": raw},
        observed_at=moment,
        source_at=source,
        condition_id=CID,
        token_id="11",
        rule_hash=RULE,
    )
