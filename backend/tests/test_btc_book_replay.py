from decimal import Decimal

from app.services.trading.btc_book_replay import replay


def frame(kind, **data):
    return {"event_type": kind, "market": "m", "timestamp": "1000", **data}


def capture(*events):
    rows = [
        {
            "kind": "start",
            "index": 0,
            "received_ms": 900,
            "condition": "m",
            "tokens": ["up", "down"],
            "tick": ".01",
            "minimum": "5",
            "end": 300,
            "venue_taker_rate": ".05",
        }
    ]
    for at, event in events:
        data = {"kind": "event", "event": event} if isinstance(event, dict) else {"kind": event}
        rows.append({"index": len(rows), "received_ms": at, **data})
    return rows


BOOK = frame(
    "book",
    asset_id="up",
    bids=[{"price": ".35", "size": "10"}],
    asks=[{"price": ".40", "size": "10"}, {"price": ".45", "size": "5"}],
)
REMOVE = frame(
    "price_change",
    price_changes=[
        {"asset_id": "up", "side": "SELL", "price": ".40", "size": "0", "best_ask": ".45"},
    ],
)
TRADE = frame("last_trade_price", asset_id="up", price=".40", size="10")


def at(rows, timestamp):
    return replay(rows, token="up", arrival_ms=timestamp, limit=Decimal(".40"), notional=Decimal(5))


def test_trade_print_does_not_change_depth_but_book_deletion_does():
    rows = capture((1000, BOOK), (1100, TRADE), (1200, REMOVE), (1400, "end"))
    est = at(rows, 1150)
    assert est["status"] == "modeled"
    assert est["trade_ticks"] == 1
    assert est["estimate"]["shares"] == 10
    assert est["venue_fee_estimate"] == Decimal(".12")
    assert at(rows, 1250)["estimate"]["shares"] == 0


def test_gap_requires_new_snapshot_and_cannot_be_repaired_with_future_data():
    rows = capture((1000, BOOK), (1100, "gap"), (1200, REMOVE), (1300, BOOK), (1400, "end"))
    assert at(rows, 1050)["reason"] == "ARRIVAL_BEFORE_DETECTED_GAP"
    assert at(rows, 1250)["reason"] == "CAPTURE_GAP"
    assert at(rows, 1350)["status"] == "modeled"


def test_missing_snapshot_sequence_and_stale_books_are_unknown():
    assert at(capture((1200, REMOVE), (1400, "end")), 1300)["reason"] == "NO_SNAPSHOT"
    rows = capture((1000, BOOK), (24000, "end"))
    assert at(rows, 23000)["reason"] == "STALE_OR_FUTURE_BOOK"
    rows[1]["index"] = 2
    assert at(rows, 1100)["reason"] == "CAPTURE_SEQUENCE_GAP"


def test_top_of_book_conflict_invalidates_depth():
    bad = frame(
        "price_change",
        price_changes=[
            {"asset_id": "up", "side": "SELL", "price": ".40", "size": "0", "best_ask": ".42"},
        ],
    )
    rows = capture((1000, BOOK), (1200, bad), (1400, "end"))
    assert at(rows, 1300)["reason"] == "INCONSISTENT_BOOK"
