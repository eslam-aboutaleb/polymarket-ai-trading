from datetime import UTC, datetime, timedelta
from decimal import Decimal

from book_research_fixtures import CID, clob_market

from app.services.market_data.replay_capture import digest
from app.services.trading.latency_book import ExecutableBookEvidence, authorize


def snapshot(now=None, **changes):
    now = now or datetime.now(UTC)
    clob = clob_market()
    clob["minimum_tick_size"] = "0.001"
    metadata = {
        "raw": {
            "clob": clob,
            "market": CID,
            "gamma": {
                "conditionId": CID,
                "active": True,
                "closed": False,
                "enableOrderBook": True,
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["11", "22"],
            },
        },
        "received_at": now.isoformat(),
        "snapshot_id": digest(clob),
    }
    raw = {
        "asset_id": "11",
        "market": CID,
        "timestamp": str(int(now.timestamp() * 1000)),
        "bids": [{"price": "0.499", "size": "1"}],
        "asks": [{"price": "0.500", "size": "1000"}],
        "hash": "",
        "tick_size": "0.001",
        "min_order_size": "1",
        "neg_risk": False,
        "last_trade_price": "0.5",
    }
    raw.update(changes)
    return raw, metadata, now


def candidate(**changes):
    raw, metadata, now = snapshot(**changes)
    return authorize(raw, metadata, "11", 50, {"base_fee": 0}, now, now, 0.02)


def ready(token="token-1", condition="market-1", cash=50):
    now = datetime.now(UTC)
    return ExecutableBookEvidence(
        status="READY",
        outcome="YES",
        reason="VALIDATED_DISPLAYED_DEPTH",
        condition_id=condition,
        token_id=token,
        received_at=now,
        source_at=now,
        expires_at=now + timedelta(seconds=1),
        shares=Decimal(str(cash)) * 2,
        limit_price=Decimal(".5"),
        authorized_cash=Decimal(str(cash)),
        worst_case_cash=Decimal(str(cash)),
        vwap=Decimal(".5"),
        tick_size=Decimal(".001"),
        neg_risk=False,
    )
