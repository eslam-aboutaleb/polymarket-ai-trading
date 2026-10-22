"""Declared synthetic dataset, never served as captured venue history."""


def experiment_payload():
    return {
        "wallet": "0x" + "a" * 40,
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": "2026-10-01T01:00:00Z",
        "funding": "1000",
        "order_notional": "100",
        "fee_bps": "0",
        "delays_seconds": [0, 60],
        "max_book_age_seconds": 60,
        "max_wait_seconds": 60,
        "dataset": {
            "source": "Synthetic test fixture (not venue history)",
            "signals": [
                {
                    "event_id": "entry",
                    "token_id": "YES",
                    "side": "BUY",
                    "first_seen_at": "2026-10-01T00:01:00Z",
                },
                {
                    "event_id": "exit",
                    "token_id": "YES",
                    "side": "SELL",
                    "first_seen_at": "2026-10-01T00:10:00Z",
                },
            ],
            "books": [
                {
                    "snapshot_id": "entry0",
                    "token_id": "YES",
                    "observed_at": "2026-10-01T00:01:00Z",
                    "source_at": "2026-10-01T00:01:00Z",
                    "bids": [{"price": ".39", "shares": "25"}],
                    "asks": [{"price": ".4", "shares": "25"}],
                },
                {
                    "snapshot_id": "entry60",
                    "token_id": "YES",
                    "observed_at": "2026-10-01T00:02:00Z",
                    "source_at": "2026-10-01T00:02:00Z",
                    "bids": [{"price": ".79", "shares": "25"}],
                    "asks": [{"price": ".8", "shares": "25"}],
                },
                {
                    "snapshot_id": "exit0",
                    "token_id": "YES",
                    "observed_at": "2026-10-01T00:10:00Z",
                    "source_at": "2026-10-01T00:10:00Z",
                    "bids": [{"price": ".7", "shares": "1000"}],
                    "asks": [{"price": ".71", "shares": "1000"}],
                },
                {
                    "snapshot_id": "exit60",
                    "token_id": "YES",
                    "observed_at": "2026-10-01T00:11:00Z",
                    "source_at": "2026-10-01T00:11:00Z",
                    "bids": [{"price": ".7", "shares": "1000"}],
                    "asks": [{"price": ".71", "shares": "1000"}],
                },
            ],
        },
    }
