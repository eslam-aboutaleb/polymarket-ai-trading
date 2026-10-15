"""Venue adapter package (plan 05).

``base`` holds the adapter contract and the registry every venue
registers into; ``kalshi`` is the reference adapter. ``manifold`` and
``predictit`` (Phase B) sit behind the same two-method contract:
``fetch_markets`` is the only method that may touch the network and
degrades to ``[]``, and ``parse_markets`` is a pure normalization that
unit tests run against a recorded fixture with zero network calls.

Money-ness differs per venue and is carried on every normalized row:
Kalshi and PredictIt are real money, Manifold is play money, so the
mismatch detector can keep a sentiment price out of an executable
signal.
"""

from app.services.venues.base import VenueAdapter, VenueRegistry, registry

__all__ = ["VenueAdapter", "VenueRegistry", "registry"]
