"""Normalized cross-venue market schema (plan 05 Phase A).

One dataclass describes a binary prediction market from any venue —
Kalshi today, Manifold/PredictIt in Phase B — in a single shape, so the
cross-venue table, the API and the UI never learn a venue's field names.

Read-only by construction: this schema carries no order, fill or
position fields, and nothing in this module performs I/O. The prices
are *signals* for comparison, never executable quotes.
``is_real_money`` flags venues whose prices are cash (a regulated
exchange) versus play money (sentiment only).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def canonical_raw_hash(raw: Any) -> str:
    """Stable content hash of one raw venue payload.

    Used to detect change between two refreshes without keeping the
    payload itself: the JSON is canonicalised (sorted keys, no
    whitespace) so the same venue record always hashes identically.
    """
    blob = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class NormalizedMarket:
    """One venue market in the shared shape used by every consumer."""

    venue: str
    market_id: str
    question: str
    outcomes: tuple[str, ...] = ("Yes", "No")
    yes_price: float | None = None
    no_price: float | None = None
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: datetime | None = None
    url: str = ""
    is_real_money: bool = True
    raw_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready snapshot (API rows, MCP payloads, cache entries)."""
        return {
            "venue": self.venue,
            "market_id": self.market_id,
            "question": self.question,
            "outcomes": list(self.outcomes),
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume_24h": self.volume_24h,
            "liquidity": self.liquidity,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "url": self.url,
            "is_real_money": self.is_real_money,
            "raw_hash": self.raw_hash,
        }
