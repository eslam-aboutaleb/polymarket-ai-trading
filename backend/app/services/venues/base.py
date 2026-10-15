"""Venue adapter contract and registry (plan 05 Phase A).

Every venue is wrapped in an adapter with exactly two methods:

* ``fetch_markets`` — the venue's one async HTTP call. It is the only
  method that may touch the network, and it degrades to ``[]`` on any
  HTTP/timeout error so a venue outage can never raise into a request
  handler or the refresh loop.
* ``parse_markets`` — a **pure** function from the raw payload to
  ``NormalizedMarket`` rows with no I/O at all. This is what the unit
  tests exercise, so a venue integration is verified offline against a
  recorded fixture.

The registry is a thread-safe name→adapter map. Registration is
idempotent: re-registering a name replaces the adapter rather than
appending, so import-time bootstrap and per-test setup can both
register the same venue without coordinating.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from app.schemas.market import NormalizedMarket


@runtime_checkable
class VenueAdapter(Protocol):
    """One prediction-market venue behind a two-method contract."""

    name: str

    async def fetch_markets(self, *, limit: int = 100) -> list[dict]:
        """Raw venue payload for the open markets, ``[]`` on failure."""
        ...

    def parse_markets(self, raw: list[dict]) -> list[NormalizedMarket]:
        """Pure normalization of ``fetch_markets`` output (no I/O)."""
        ...


class VenueRegistry:
    """Thread-safe, idempotent name → adapter map."""

    def __init__(self) -> None:
        self._adapters: dict[str, VenueAdapter] = {}
        self._lock = threading.Lock()

    def register(self, name: str, adapter: VenueAdapter) -> None:
        """Add or replace one adapter. Never grows the map on repeat calls."""
        with self._lock:
            self._adapters[name] = adapter

    def get(self, name: str) -> VenueAdapter | None:
        with self._lock:
            return self._adapters.get(name)

    def all(self) -> list[VenueAdapter]:
        """Every registered adapter, ordered by name for determinism."""
        with self._lock:
            return [self._adapters[name] for name in sorted(self._adapters)]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._adapters)


# Process-wide registry shared by the refresh service, the routes and tests.
registry = VenueRegistry()
