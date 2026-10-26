"""Offline deterministic book-cost demo. No network, database or credentials."""

import json
import sys
from datetime import UTC, datetime, timedelta

from app.services.market_data.binary_book import (
    BookEvent,
    Level,
    MarketSpec,
    WalkRequest,
    reconstruct,
    walk_depth,
)


def main():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    spec = MarketSpec(
        condition_id="demo",
        yes_token="11",  # noqa: S106 - public outcome ID, not a credential
        no_token="22",  # noqa: S106 - public outcome ID
        rule_hash="demo-rules",
        metadata_id="demo-meta",
        received_at=now - timedelta(seconds=1),
        active=True,
        tick="0.01",
        minimum_shares="1",
    )
    event = BookEvent(
        snapshot_id="demo-snapshot",
        token_id="11",  # noqa: S106 - public outcome ID
        condition_id="demo",
        rule_hash="demo-rules",
        received_at=now,
        source_at=now,
        connection_id="demo",
        kind="snapshot",
        bids=[Level(price="0.4", shares="10")],
        asks=[Level(price="0.6", shares="20"), Level(price="0.5", shares="10")],
    )
    book = reconstruct(spec, "11", [event], now)
    estimate = walk_depth(
        spec, book, WalkRequest(side="BUY", amount="20", fee_bps="100", fee_version="demo-flat-v1")
    )
    sys.stdout.write(
        json.dumps(
            {
                "classification": "synthetic_shadow_fixture",
                "book": book.model_dump(mode="json"),
                "estimate": estimate.model_dump(mode="json"),
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
