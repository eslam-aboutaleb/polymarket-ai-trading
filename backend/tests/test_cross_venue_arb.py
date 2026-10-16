"""Cross-venue arbitrage, venue adapters and wallet clustering (plan 05 Phase B).

The bar for this phase is that every piece of signal-producing math is
provable **offline**:

* the two new venue parsers run on recorded fixtures with zero network
  calls (Manifold's fixture came from a live ``/v0/markets`` payload;
  PredictIt's endpoint answers a plain client with a Cloudflare ``403``,
  so its fixture follows PredictIt's *documented* market-data shape and
  the live reachability gap is asserted as a degradation, not hidden);
* every fetch failure mode — 404, timeout, transport error, blocked
  endpoint — returns ``[]`` and never raises, and a failure is never
  cached;
* mismatch math runs on synthetic rows, including the ``is_real_money``
  exclusion that keeps a play-money price out of an executable signal;
* the cluster graph returns a stable grouping for a fixed synthetic
  event set;
* both new routes reject an unauthenticated caller.

No test touches the network: venue traffic goes through
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.base import Base
from app.models.user import User
from app.models.venue_market import VenueMarket
from app.models.whale_event import WhaleEvent
from app.security.auth import create_access_token
from app.services.analytics.cross_venue_arb import (
    MATCH_EXACT,
    MATCH_FUZZY,
    find_mismatches,
    get_intra_polymarket_signals,
    match_questions,
    mismatches_from_rows,
    token_overlap_score,
)
from app.services.analytics.wallet_clustering import clusters_from_events
from app.services.venues.manifold import ManifoldAdapter, parse_manifold_markets
from app.services.venues.predictit import PredictItAdapter, parse_predictit_markets
from app.utils.database import get_db
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20

# ── Recorded fixtures ──────────────────────────────────────────
# Three binary markets are verbatim from
# GET https://api.manifold.markets/v0/markets?limit=60 on 2026-10-12;
# the rest are hand-built rows for the skip paths (non-binary outcome
# type, resolved market, missing probability, unlinkable id).
RECORDED_MANIFOLD_MARKETS = [
    {
        "id": "pc2AtN2A86",
        "creatorId": "igSA7yIeH3ZEnVtMjaJvIEOfGsw1",
        "creatorUsername": "vi",
        "createdTime": 1791834567883,
        "closeTime": 1792497540000,
        "question": (
            "Will the address for math.eg.rego.re receive more than $50 before Oct 20 UTC-12?"
        ),
        "slug": "will-the-address-for-mathegregore-r-ESg0Lggz8Q",
        "url": "https://manifold.markets/vi/will-the-address-for-mathegregore-r-ESg0Lggz8Q",
        "pool": {"NO": 106.64214900238497, "YES": 1749.918320497707},
        "probability": 0.19598950619656513,
        "p": 0.8,
        "totalLiquidity": 1000,
        "outcomeType": "BINARY",
        "mechanism": "cpmm-1",
        "volume": 751.9800917306429,
        "volume24Hours": 0,
        "isResolved": False,
        "uniqueBettorCount": 2,
    },
    {
        "id": "E89LAy6Ns2",
        "creatorId": "Jack1",
        "creatorUsername": "Jack1",
        "createdTime": 1791834567883,
        "closeTime": 1793750340000,
        "question": "Will Roy Cooper win Transylvania?",
        "slug": "will-roy-cooper-win-transylvania",
        "url": "https://manifold.markets/Jack1/will-roy-cooper-win-transylvania",
        "pool": {"NO": 520.0, "YES": 480.0},
        "probability": 0.3700000000000003,
        "p": 0.48000000000000004,
        "totalLiquidity": 1000,
        "outcomeType": "BINARY",
        "mechanism": "cpmm-1",
        "volume": 447.43796207003805,
        "volume24Hours": 647.437962070038,
        "isResolved": False,
        "uniqueBettorCount": 12,
    },
    {
        "id": "ZhsSCZq8Qu",
        "creatorId": "Web3ICP",
        "creatorUsername": "Web3ICP",
        "createdTime": 1791834567883,
        "closeTime": 1792342620000,
        "question": (
            "NFL Week 6: Will the total combined score in the New York Jets vs. "
            "New England Patriots game be OVER 44.5 points?"
        ),
        "slug": "nfl-week-6-will-the-total-combined-EtySEEpEP8",
        "url": "https://manifold.markets/Web3ICP/nfl-week-6-will-the-total-combined-EtySEEpEP8",
        "probability": 0.4697247706422019,
        "p": 0.5000000000000001,
        "totalLiquidity": 1000,
        "outcomeType": "BINARY",
        "mechanism": "cpmm-1",
        "volume": 62.5,
        "volume24Hours": 62.5,
        "isResolved": False,
        "uniqueBettorCount": 3,
    },
    # Multi-outcome question: no single yes price, so it cannot be
    # normalized onto the shared binary shape.
    {
        "id": "U269nhhUNQ",
        "question": "Gas Price on October 19",
        "slug": "gas-price-on-october-19",
        "url": "https://manifold.markets/x/gas-price-on-october-19",
        "outcomeType": "MULTIPLE_CHOICE",
        "volume24Hours": 244.08660757703836,
        "totalLiquidity": 1000,
        "closeTime": 1792454340000,
        "isResolved": False,
    },
    # Resolved: the price is final, not a quote.
    {
        "id": "RESOLVED01",
        "question": "Will a resolved market be normalized?",
        "slug": "will-a-resolved-market-be-normalized",
        "outcomeType": "BINARY",
        "probability": 1.0,
        "volume24Hours": 10.0,
        "totalLiquidity": 10.0,
        "closeTime": 1792454340000,
        "isResolved": True,
    },
    # No probability at all.
    {
        "id": "NOPROB01",
        "question": "Will an unquoted binary market be normalized?",
        "slug": "will-an-unquoted-binary-market-be-normalized",
        "outcomeType": "BINARY",
        "volume24Hours": 10.0,
        "totalLiquidity": 10.0,
        "closeTime": 1792454340000,
        "isResolved": False,
    },
    # No id: nothing to link back to.
    {
        "question": "Will an unidentified market be normalized?",
        "outcomeType": "BINARY",
        "probability": 0.5,
        "totalLiquidity": 10.0,
        "isResolved": False,
    },
]

# PredictIt's public ``/api/marketdata/all/`` shape (a plain HTTP client
# gets a Cloudflare 403 from it, so this fixture is built from the
# documented schema rather than recorded live — see the adapter's
# docstring for the jurisdiction and access caveats).
PREDICTIT_MARKETS = [
    {
        "id": 7456,
        "name": "Which party will win the 2028 U.S. presidential election?",
        "shortName": "2028 presidential winner",
        "status": "Open",
        "dateEnd": "2028-11-05T00:00:00",
        "contracts": [
            {
                "id": 18516,
                "name": "Democratic",
                "shortName": "Democratic",
                "status": "Open",
                "lastTradePrice": 0.52,
                "bestBuyYesCost": 0.545,
                "bestBuyNoCost": 0.475,
                "bestSellYesCost": 0.535,
                "bestSellNoCost": 0.455,
            },
            {
                "id": 18517,
                "name": "Republican",
                "shortName": "Republican",
                "status": "Open",
                "lastTradePrice": 0.45,
                "bestBuyYesCost": 0.50,
                "bestBuyNoCost": 0.53,
                "bestSellYesCost": 0.47,
                "bestSellNoCost": 0.50,
            },
            {
                # Closed: no longer priceable.
                "id": 18518,
                "name": "Closed contract",
                "status": "Closed",
                "lastTradePrice": 0.1,
                "bestBuyYesCost": 0.2,
                "bestSellYesCost": 0.15,
            },
            {
                # No price of any kind.
                "id": 18519,
                "name": "Unquoted contract",
                "status": "Open",
            },
        ],
    },
    {
        "id": 8221,
        "name": "Will the Fed cut rates at the next meeting?",
        "shortName": "Fed cut",
        "status": "Open",
        "dateEnd": "2026-12-16T00:00:00",
        "contracts": [
            {
                "id": 27410,
                "name": "Yes",
                "shortName": "Yes",
                "status": "Open",
                # Top of book only on one side.
                "bestBuyYesCost": 0.31,
                "lastTradePrice": 0.3,
            }
        ],
    },
    {
        # Closed market: skipped whole.
        "id": 9999,
        "name": "Will a closed market be normalized?",
        "status": "Closed",
        "contracts": [
            {"id": 1, "name": "Yes", "status": "Open", "bestBuyYesCost": 0.5},
        ],
    },
]


def _auth_headers() -> dict:
    token = create_access_token({"user_id": 1, "sub": WALLET, "is_admin": False})
    return {"Authorization": f"Bearer {token}"}


def _run_async(coro):
    return asyncio.run(coro)


def _patch_client(module, handler):
    """Point a venue module's ``httpx.AsyncClient`` at a MockTransport.

    The real class is captured before the patch so the factory itself
    does not recurse into the patched name.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("headers", None)
        return real_client(transport=transport, timeout=15.0)

    patcher = patch(f"{module}.httpx.AsyncClient", factory)
    patcher.start()
    return patcher


def _venue_row(
    venue: str,
    question: str,
    yes: float | None,
    *,
    market_id: str | None = None,
    liquidity: float = 100.0,
    is_real_money: bool = True,
    fetched_at: str = "2026-10-12T12:00:00+00:00",
) -> dict:
    """One serialized ``venue_markets`` row (the mismatch function's input)."""
    return {
        "venue": venue,
        "market_id": market_id or f"{venue}-{abs(hash(question)) % 100000}",
        "question": question,
        "yes_price": yes,
        "no_price": None if yes is None else round(1.0 - yes, 6),
        "outcomes": ["Yes", "No"],
        "volume_24h": 10.0,
        "liquidity": liquidity,
        "end_date": None,
        "url": f"https://example.test/{venue}",
        "is_real_money": is_real_money,
        "fetched_at": fetched_at,
    }


def _whale_event(
    wallet: str,
    market_id: str,
    side: str,
    *,
    notional: float = 15000.0,
    detected_at: datetime | None = None,
) -> dict:
    return {
        "wallet": wallet.lower(),
        "market_id": market_id,
        "side": side,
        "token_id": "123",
        "event_type": "transfer",
        "notional": notional,
        "detected_at": detected_at or utc_now(),
    }


class ManifoldParserTests(unittest.TestCase):
    """The pure parser against the recorded fixture — zero network calls."""

    def setUp(self):
        self.markets = parse_manifold_markets(RECORDED_MANIFOLD_MARKETS)

    def test_only_priceable_binary_markets_survive(self):
        self.assertEqual(
            [m.market_id for m in self.markets],
            ["pc2AtN2A86", "E89LAy6Ns2", "ZhsSCZq8Qu"],
        )
        for market in self.markets:
            self.assertEqual(market.venue, "manifold")
            self.assertEqual(market.outcomes, ("Yes", "No"))

    def test_play_money_flag_and_complement(self):
        first = self.markets[0]
        self.assertFalse(first.is_real_money)
        self.assertAlmostEqual(first.yes_price, 0.19598950619656513)
        self.assertAlmostEqual(first.no_price, 1.0 - first.yes_price)

    def test_prices_volume_and_liquidity(self):
        second = self.markets[1]
        self.assertAlmostEqual(second.yes_price, 0.37)
        self.assertAlmostEqual(second.volume_24h, 647.437962070038)
        # Manifold's liquidity unit is Mana, not dollars.
        self.assertAlmostEqual(second.liquidity, 1000.0)

    def test_close_time_is_epoch_milliseconds(self):
        first = self.markets[0]
        self.assertEqual(
            first.end_date.isoformat(),
            datetime.fromtimestamp(1792497540000 / 1000, tz=UTC).isoformat(),
        )
        third = self.markets[2]
        self.assertEqual(
            third.end_date.isoformat(),
            datetime.fromtimestamp(1792342620000 / 1000, tz=UTC).isoformat(),
        )
        # Second-precision payloads are tolerated too.
        market = parse_manifold_markets([dict(RECORDED_MANIFOLD_MARKETS[0], closeTime=1792342620)])[
            0
        ]
        self.assertEqual(
            market.end_date.isoformat(),
            datetime.fromtimestamp(1792342620, tz=UTC).isoformat(),
        )

    def test_url_and_hash(self):
        third = self.markets[2]
        self.assertEqual(third.url, RECORDED_MANIFOLD_MARKETS[2]["url"])
        self.assertTrue(third.raw_hash)
        changed = dict(RECORDED_MANIFOLD_MARKETS[2], probability=0.5)
        self.assertNotEqual(parse_manifold_markets([changed])[0].raw_hash, third.raw_hash)

    def test_malformed_rows_are_skipped_not_fatal(self):
        self.assertNotIn("NOPROB01", [m.market_id for m in self.markets])
        self.assertNotIn("RESOLVED01", [m.market_id for m in self.markets])
        self.assertNotIn("U269nhhUNQ", [m.market_id for m in self.markets])

    def test_missing_numeric_fields_default_to_zero(self):
        market = parse_manifold_markets(
            [dict(RECORDED_MANIFOLD_MARKETS[0], totalLiquidity=None, volume24Hours=None)]
        )[0]
        self.assertEqual(market.liquidity, 0.0)
        self.assertEqual(market.volume_24h, 0.0)

    def test_bad_close_time_is_not_fatal(self):
        market = parse_manifold_markets(
            [dict(RECORDED_MANIFOLD_MARKETS[0], closeTime="not-a-timestamp")]
        )[0]
        self.assertIsNone(market.end_date)

    def test_adapter_delegates_to_the_pure_function(self):
        adapter = ManifoldAdapter()
        self.assertEqual(adapter.name, "manifold")
        self.assertEqual(
            [m.market_id for m in adapter.parse_markets(RECORDED_MANIFOLD_MARKETS)],
            [m.market_id for m in self.markets],
        )


class PredictItParserTests(unittest.TestCase):
    """The pure parser against PredictIt's documented market-data shape."""

    def setUp(self):
        self.markets = parse_predictit_markets(PREDICTIT_MARKETS)

    def test_one_row_per_open_contract(self):
        self.assertEqual(
            [m.market_id for m in self.markets],
            ["18516", "18517", "27410"],
        )
        for market in self.markets:
            self.assertEqual(market.venue, "predictit")
            # PredictIt is real money, unlike Manifold.
            self.assertTrue(market.is_real_money)

    def test_yes_price_comes_from_the_book_mid(self):
        democratic = self.markets[0]
        self.assertAlmostEqual(democratic.yes_price, (0.535 + 0.545) / 2)
        self.assertAlmostEqual(democratic.no_price, (0.455 + 0.475) / 2)

    def test_one_sided_book_falls_back_to_the_buy_price(self):
        fed = self.markets[2]
        self.assertAlmostEqual(fed.yes_price, 0.31)
        self.assertAlmostEqual(fed.no_price, 0.69)

    def test_multi_contract_question_carries_the_contract_name(self):
        # Without it every contract of one market would normalize to the
        # same key and collapse into one fake mismatch against itself.
        self.assertEqual(
            self.markets[0].question,
            "Which party will win the 2028 U.S. presidential election?: Democratic",
        )
        self.assertEqual(
            self.markets[1].question,
            "Which party will win the 2028 U.S. presidential election?: Republican",
        )

    def test_single_contract_market_is_the_binary_question(self):
        self.assertEqual(self.markets[2].question, "Will the Fed cut rates at the next meeting?")

    def test_url_end_date_and_hash(self):
        democratic = self.markets[0]
        self.assertEqual(
            democratic.url,
            "https://www.predictit.org/markets/detail/7456",
        )
        self.assertEqual(democratic.end_date.isoformat(), "2028-11-05T00:00:00+00:00")
        self.assertTrue(democratic.raw_hash)

    def test_unknown_volume_and_depth_are_zero_not_guessed(self):
        for market in self.markets:
            self.assertEqual(market.volume_24h, 0.0)
            self.assertEqual(market.liquidity, 0.0)

    def test_closed_and_unquoted_contracts_are_skipped(self):
        questions = [m.question for m in self.markets]
        self.assertNotIn(
            "Which party will win the 2028 U.S. presidential election?: Closed contract",
            questions,
        )
        self.assertFalse(any("Unquoted" in question for question in questions))

    def test_bare_list_payload_is_accepted(self):
        markets = parse_predictit_markets(PREDICTIT_MARKETS)
        self.assertEqual(len(markets), 3)

    def test_adapter_delegates_to_the_pure_function(self):
        adapter = PredictItAdapter()
        self.assertEqual(adapter.name, "predictit")
        self.assertEqual(
            [m.market_id for m in adapter.parse_markets(PREDICTIT_MARKETS)],
            [m.market_id for m in self.markets],
        )


class VenueFetchDegradationTests(unittest.TestCase):
    """Every failure mode returns ``[]`` and never raises."""

    def _clear(self):
        from app.services.venues import manifold as manifold_module
        from app.services.venues import predictit as predictit_module

        self.modules = {"manifold": manifold_module, "predictit": predictit_module}
        for module in self.modules.values():
            module.CACHE.clear()

    def _fetch(self, venue: str, handler, *, limit: int = 50) -> list[dict]:
        adapter = ManifoldAdapter() if venue == "manifold" else PredictItAdapter()
        patcher = _patch_client(f"app.services.venues.{venue}", handler)
        self.addCleanup(patcher.stop)
        return _run_async(adapter.fetch_markets(limit=limit))

    def setUp(self):
        self._clear()
        self.addCleanup(self._clear)

    def test_manifold_404_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        self.assertEqual(self._fetch("manifold", handler), [])

    def test_predictit_403_is_treated_as_a_dark_venue(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "cloudflare"})

        self.assertEqual(self._fetch("predictit", handler), [])

    def test_manifold_timeout_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        self.assertEqual(self._fetch("manifold", handler), [])

    def test_predictit_transport_error_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        self.assertEqual(self._fetch("predictit", handler), [])

    def test_manifold_malformed_payload_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"markets": "not-a-list"})

        self.assertEqual(self._fetch("manifold", handler), [])

    def test_predictit_successful_payload_is_parsed_by_the_caller(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/api/marketdata/all/", str(request.url))
            return httpx.Response(200, json={"markets": PREDICTIT_MARKETS})

        raw = self._fetch("predictit", handler)
        self.assertEqual(len(raw), 3)
        self.assertEqual(raw[0]["contracts"][0]["id"], 18516)

    def test_manifold_failure_is_not_cached(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "unavailable"})

        self.assertEqual(self._fetch("manifold", handler), [])
        self.assertIsNone(self.modules["manifold"].CACHE.get("markets:50"))

    def test_predictit_limit_caps_the_bundle(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"markets": PREDICTIT_MARKETS})

        # PredictIt is all-or-nothing, so the cap is applied on this side.
        self.assertEqual(len(self._fetch("predictit", handler, limit=2)), 2)


class MismatchMathTests(unittest.TestCase):
    """Pure mismatch math over synthetic rows."""

    QUESTION = "Will the Fed cut rates at the December meeting?"
    OTHER_QUESTION = "Will the Fed raise rates at the December meeting?"

    def _pair(self, cheap: float, rich: float, **kwargs) -> list[dict]:
        return [
            _venue_row("kalshi", self.QUESTION, cheap, liquidity=kwargs.get("liquidity", 100.0)),
            _venue_row(
                "predictit", self.QUESTION, rich, liquidity=kwargs.get("rich_liquidity", 80.0)
            ),
        ]

    def test_exact_match_reports_the_price_gap(self):
        mismatches = mismatches_from_rows(self._pair(0.42, 0.55))
        self.assertEqual(len(mismatches), 1)
        mismatch = mismatches[0]
        self.assertEqual(mismatch["match_type"], MATCH_EXACT)
        self.assertEqual(mismatch["venues"], ["kalshi", "predictit"])
        self.assertAlmostEqual(mismatch["low_price"], 0.42)
        self.assertAlmostEqual(mismatch["high_price"], 0.55)
        self.assertAlmostEqual(mismatch["edge"], 0.13, places=4)
        self.assertAlmostEqual(mismatch["edge_pct"], 13.0, places=2)
        self.assertTrue(mismatch["executable_signal"])

    def test_both_questions_are_surfaced_verbatim(self):
        rows = [
            _venue_row("kalshi", "Will the Fed cut rates at the December meeting?", 0.42),
            _venue_row("predictit", "Will the Fed CUT rates at the December meeting?", 0.55),
        ]
        mismatch = mismatches_from_rows(rows)[0]
        self.assertEqual(
            mismatch["questions"]["kalshi"],
            "Will the Fed cut rates at the December meeting?",
        )
        self.assertEqual(
            mismatch["questions"]["predictit"],
            "Will the Fed CUT rates at the December meeting?",
        )
        self.assertEqual(mismatch["cheap"]["question"], mismatch["questions"]["kalshi"])
        self.assertEqual(mismatch["rich"]["question"], mismatch["questions"]["predictit"])

    def test_below_min_edge_is_not_a_mismatch(self):
        self.assertEqual(mismatches_from_rows(self._pair(0.50, 0.52)), [])
        # A tighter threshold still sees it.
        self.assertEqual(len(mismatches_from_rows(self._pair(0.50, 0.52), min_edge=0.01)), 1)

    def test_min_liquidity_applies_to_the_weaker_leg(self):
        rows = self._pair(0.42, 0.55, liquidity=100.0, rich_liquidity=5.0)
        self.assertEqual(mismatches_from_rows(rows, min_liquidity=10.0), [])
        self.assertEqual(len(mismatches_from_rows(rows, min_liquidity=5.0)), 1)

    def test_single_venue_is_never_a_mismatch(self):
        rows = [
            _venue_row("kalshi", self.QUESTION, 0.42),
            _venue_row("kalshi", self.QUESTION, 0.62),
        ]
        self.assertEqual(mismatches_from_rows(rows), [])

    def test_play_money_leg_is_excluded_by_default(self):
        rows = [
            _venue_row("kalshi", self.QUESTION, 0.42),
            _venue_row("manifold", self.QUESTION, 0.61, is_real_money=False),
        ]
        self.assertEqual(mismatches_from_rows(rows), [])
        included = mismatches_from_rows(rows, include_play_money=True)
        self.assertEqual(len(included), 1)
        mismatch = included[0]
        self.assertFalse(mismatch["executable_signal"])
        self.assertFalse(mismatch["play_money_only"])
        self.assertEqual(mismatch["play_money_venues"], ["manifold"])
        self.assertIn("sentiment", mismatch["play_money_reason"])

    def test_two_play_money_legs_are_sentiment_only(self):
        rows = [
            _venue_row("manifold", self.QUESTION, 0.30, is_real_money=False, market_id="m1"),
            _venue_row("manifold-b", self.QUESTION, 0.55, is_real_money=False, market_id="m2"),
        ]
        mismatch = mismatches_from_rows(rows, include_play_money=True)[0]
        self.assertTrue(mismatch["play_money_only"])
        self.assertFalse(mismatch["executable_signal"])

    def test_rows_without_a_price_are_not_comparable(self):
        rows = [
            _venue_row("kalshi", self.QUESTION, None),
            _venue_row("predictit", self.QUESTION, 0.6),
        ]
        self.assertEqual(mismatches_from_rows(rows), [])

    def test_horizon_gap_is_surfaced_but_not_filtered(self):
        rows = self._pair(0.42, 0.55)
        rows[0]["end_date"] = "2026-12-16T00:00:00+00:00"
        rows[1]["end_date"] = "2027-01-20T00:00:00+00:00"
        mismatch = mismatches_from_rows(rows)[0]
        self.assertAlmostEqual(mismatch["end_date_delta_days"], 35.0, places=1)
        self.assertTrue(mismatch["horizon_mismatch"])

    def test_same_horizon_is_not_flagged(self):
        rows = self._pair(0.42, 0.55)
        for row in rows:
            row["end_date"] = "2026-12-16T00:00:00+00:00"
        mismatch = mismatches_from_rows(rows)[0]
        self.assertEqual(mismatch["end_date_delta_days"], 0.0)
        self.assertFalse(mismatch["horizon_mismatch"])

    def test_unknown_horizon_is_none(self):
        mismatch = mismatches_from_rows(self._pair(0.42, 0.55))[0]
        self.assertIsNone(mismatch["end_date_delta_days"])
        self.assertFalse(mismatch["horizon_mismatch"])

    def test_fuzzy_match_when_the_questions_differ_in_wording(self):
        rows = [
            _venue_row("kalshi", "Will Bitcoin close above $100,000 on Dec 31?", 0.40),
            _venue_row("predictit", "Will Bitcoin close above $100,000 by December 31?", 0.58),
        ]
        mismatch = mismatches_from_rows(rows)[0]
        self.assertEqual(mismatch["match_type"], MATCH_FUZZY)
        self.assertGreaterEqual(mismatch["match_score"], 0.75)
        self.assertIn("bitcoin", mismatch["shared_tokens"])

    def test_same_series_brackets_do_not_match(self):
        # Every token matches except the threshold, and the threshold is
        # exactly what makes them different markets.
        rows = [
            _venue_row("manifold-a", "Will the total combined score be OVER 44.5 points?", 0.30),
            _venue_row("manifold-b", "Will the total combined score be OVER 43.5 points?", 0.55),
        ]
        self.assertEqual(mismatches_from_rows(rows, include_play_money=True), [])

    def test_opposite_sides_of_one_market_do_not_match(self):
        self.assertIsNone(
            match_questions(
                "Will the Fed cut rates in December?", "Will the Fed hike rates in December?"
            ),
        )

    def test_boilerplate_overlap_is_rejected(self):
        self.assertIsNone(match_questions("Will France win?", "Will Germany win?"))

    def test_overlap_score_bounds(self):
        self.assertEqual(
            token_overlap_score("Will the Fed cut rates?", "Will the Fed cut rates?"), 1.0
        )
        self.assertEqual(token_overlap_score("bitcoin", "kalshi weather"), 0.0)

    def test_results_are_ordered_by_edge_then_liquidity(self):
        rows = self._pair(0.42, 0.55) + self._pair(0.50, 0.70)
        mismatches = mismatches_from_rows(rows)
        self.assertEqual(len(mismatches), 4)
        self.assertEqual(
            [round(mismatch["edge"], 4) for mismatch in mismatches],
            sorted((round(mismatch["edge"], 4) for mismatch in mismatches), reverse=True),
        )

    def test_limit_caps_the_result(self):
        rows = self._pair(0.42, 0.55) + self._pair(0.50, 0.70)
        self.assertEqual(len(mismatches_from_rows(rows, limit=1)), 1)

    def test_non_mapping_rows_are_ignored(self):
        self.assertEqual(mismatches_from_rows(["not-a-row"]), [])


class ScanGuardTests(unittest.TestCase):
    """The mismatch scan stays bounded on a production-sized table."""

    VENUES = ("kalshi", "manifold", "predictit")

    def _table(self, rows_per_venue: int, question: str | None = None) -> list[dict]:
        question = question or "Will the candidate win the 2028 election by more than 5 points?"
        return [
            _venue_row(venue, question, 0.3 + 0.4 * ((index % 7) / 7), market_id=f"{venue}-{index}")
            for venue in self.VENUES
            for index in range(rows_per_venue)
        ]

    def test_every_pair_is_cross_venue(self):
        mismatches = mismatches_from_rows(self._table(100), min_edge=0.02)
        self.assertTrue(mismatches)
        for mismatch in mismatches:
            self.assertEqual(len(mismatch["venues"]), 2)
            self.assertNotEqual(mismatch["cheap"]["venue"], mismatch["rich"]["venue"])

    def test_duplicate_questions_still_match_by_exact_key(self):
        mismatches = mismatches_from_rows(self._table(50), min_edge=0.02)
        self.assertTrue(mismatches)
        self.assertTrue(all(mismatch["match_type"] == MATCH_EXACT for mismatch in mismatches))

    def test_the_candidate_scan_is_bounded(self):
        from app.services.analytics.cross_venue_arb import MAX_CANDIDATES_PER_ROW, _candidate_pairs

        rows = self._table(30)
        pairs = _candidate_pairs(rows)
        self.assertTrue(pairs)
        self.assertLessEqual(len(pairs), MAX_CANDIDATES_PER_ROW * len(rows))
        # Only cross-venue pairs, with the cheaper side first.
        for low, high, _match in pairs:
            self.assertNotEqual(low["venue"], high["venue"])
            self.assertLessEqual(low["yes_price"], high["yes_price"])
        # The capped slots keep the matches that matter rather than
        # filling up with same-venue rows.
        self.assertTrue(all(match["match_type"] == MATCH_EXACT for _low, _high, match in pairs))
        self.assertGreaterEqual(len({pair[0]["venue"] for pair in pairs}), 2)

    def test_question_facts_are_memoized(self):
        from app.services.analytics.cross_venue_arb import _question

        first = _question("Will the Fed cut rates in December?")
        second = _question("Will the Fed cut rates in December?")
        self.assertIs(first, second)

    """The new module reads, and never reimplements, the existing detector."""

    def test_spots_are_read_from_arbitrage_service(self):
        from app.services.analytics import arbitrage_service

        spot = {
            "type": "complement",
            "market_id": "0xcond",
            "market_title": "Will the Fed cut rates in December?",
            "prices": [0.45, 0.5],
            "total_cost": 0.95,
            "profit_pct": 5.0,
            "detected_at": "2026-10-12T12:00:00+00:00",
        }
        with patch.object(arbitrage_service, "_arb_opportunities", [spot, "not-a-dict"]):
            signals = get_intra_polymarket_signals()
        self.assertEqual(signals, [spot])
        # Non-dict rows are dropped, and the cap applies inside the patch.
        with patch.object(arbitrage_service, "_arb_opportunities", [spot, spot, spot]):
            self.assertEqual(len(get_intra_polymarket_signals(limit=1)), 1)

    def test_empty_list_is_not_an_error(self):
        """No cached opportunities yields no signals and no exception.

        The fixture is pinned rather than assumed: ``_arb_opportunities``
        is module-level state the arbitrage monitor appends to, so
        asserting on whatever a previously-run test left there makes
        this pass or fail on execution order.
        """
        from app.services.analytics import arbitrage_service

        with patch.object(arbitrage_service, "_arb_opportunities", []):
            self.assertEqual(get_intra_polymarket_signals(), [])


class FindMismatchesDbTests(unittest.TestCase):
    """``find_mismatches`` over an in-memory ``venue_markets`` table."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        self.engine.dispose()

    def _seed(
        self,
        venue: str,
        question: str,
        yes: float,
        liquidity: float = 100.0,
        *,
        is_real_money: bool = True,
    ) -> None:
        self.db.add(
            VenueMarket(
                venue=venue,
                market_id=f"{venue}-{abs(hash(question)) % 100000}",
                question=question,
                prices_json=json.dumps({"yes": yes, "no": 1 - yes, "is_real_money": is_real_money}),
                volume_24h=10.0,
                liquidity=liquidity,
            )
        )
        self.db.commit()

    def test_mismatch_found_across_two_cached_venues(self):
        self._seed("kalshi", "Will the Fed cut rates in December?", 0.42, 900.0)
        self._seed("predictit", "Will the Fed cut rates in December?", 0.55, 100.0)
        mismatches = find_mismatches(self.db)
        self.assertEqual(len(mismatches), 1)
        self.assertAlmostEqual(mismatches[0]["edge"], 0.13, places=4)
        self.assertEqual(mismatches[0]["cheap"]["venue"], "kalshi")
        self.assertEqual(mismatches[0]["rich"]["venue"], "predictit")

    def test_manifold_row_alone_produces_nothing_executable(self):
        self._seed("kalshi", "Will the Fed cut rates in December?", 0.42, 900.0)
        self._seed(
            "manifold", "Will the Fed cut rates in December?", 0.61, 500.0, is_real_money=False
        )
        self.assertEqual(find_mismatches(self.db), [])
        flagged = find_mismatches(self.db, include_play_money=True)
        self.assertEqual(len(flagged), 1)
        self.assertFalse(flagged[0]["executable_signal"])
        self.assertEqual(flagged[0]["play_money_venues"], ["manifold"])

    def test_empty_table_returns_empty_list(self):
        self.assertEqual(find_mismatches(self.db), [])


class WalletClusterTests(unittest.TestCase):
    """The co-occurrence graph on a synthetic event set."""

    ALPHA = "0x" + "11" * 20
    BETA = "0x" + "22" * 20
    GAMMA = "0x" + "33" * 20
    DELTA = "0x" + "44" * 20

    def _clustered_events(self) -> list[dict]:
        """ALPHA and BETA share two markets on the buy side."""
        # Anchor to "now" so the default 24h window always contains the
        # events regardless of the wall-clock date the suite runs on.
        base = utc_now()
        events = []
        for market in ("m1", "m2"):
            events.append(_whale_event(self.ALPHA, market, "buy", detected_at=base))
            events.append(_whale_event(self.BETA, market, "buy", detected_at=base))
        # One shared market is not enough at min_co_occurrence=2.
        events.append(_whale_event(self.GAMMA, "m4", "sell", detected_at=base))
        events.append(_whale_event(self.DELTA, "m4", "sell", detected_at=base))
        # Same market, opposite side: never co-occurrence.
        events.append(_whale_event(self.ALPHA, "m3", "sell", detected_at=base))
        events.append(_whale_event(self.BETA, "m3", "buy", detected_at=base))
        return events

    def test_stable_grouping_for_a_fixed_event_set(self):
        first = clusters_from_events(self._clustered_events())
        second = clusters_from_events(reversed(self._clustered_events()))
        self.assertEqual(first, second)

    def test_shared_markets_form_a_cluster(self):
        clusters = clusters_from_events(self._clustered_events())
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster["wallets"], sorted([self.ALPHA, self.BETA]))
        self.assertEqual(cluster["size"], 2)
        self.assertEqual(cluster["cluster_id"], "c1")
        self.assertEqual(cluster["co_occurrence"], 2)
        self.assertEqual(cluster["distinct_markets"], 2)
        self.assertEqual(
            [bucket["market_id"] for bucket in cluster["shared_markets"]], ["m1", "m2"]
        )
        self.assertEqual({bucket["side"] for bucket in cluster["shared_markets"]}, {"buy"})
        self.assertAlmostEqual(cluster["total_notional"], 90000.0)

    def test_single_shared_market_is_not_an_edge(self):
        lone = clusters_from_events(
            [
                _whale_event(self.GAMMA, "m4", "sell"),
                _whale_event(self.DELTA, "m4", "sell"),
            ]
        )
        self.assertEqual(lone, [])
        # With min_co_occurrence=1 the same event set groups.
        self.assertEqual(
            len(
                clusters_from_events(
                    [
                        _whale_event(self.GAMMA, "m4", "sell"),
                        _whale_event(self.DELTA, "m4", "sell"),
                    ],
                    min_co_occurrence=1,
                )
            ),
            1,
        )

    def test_opposite_sides_do_not_cluster(self):
        clusters = clusters_from_events(self._clustered_events())
        self.assertEqual(len(clusters), 1)
        # m3 never surfaces: the cluster's members traded it on opposite
        # sides, which is not co-occurrence.
        self.assertEqual([b["market_id"] for b in clusters[0]["shared_markets"]], ["m1", "m2"])

    def test_transitive_edge_joins_one_cluster(self):
        events = [
            _whale_event(self.ALPHA, "m1", "buy"),
            _whale_event(self.BETA, "m1", "buy"),
            _whale_event(self.BETA, "m2", "sell"),
            _whale_event(self.GAMMA, "m2", "sell"),
        ]
        clusters = clusters_from_events(events, min_co_occurrence=1)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["size"], 3)
        self.assertEqual(clusters[0]["distinct_markets"], 2)
        self.assertEqual(clusters[0]["cluster_id"], "c1")

    def test_window_excludes_old_events(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        events = [
            # Two shared markets outside the window, two inside it.
            _whale_event(self.ALPHA, "m1", "buy", detected_at=now - timedelta(hours=48)),
            _whale_event(self.BETA, "m1", "buy", detected_at=now - timedelta(hours=48)),
            _whale_event(self.ALPHA, "m5", "buy", detected_at=now - timedelta(hours=48)),
            _whale_event(self.BETA, "m5", "buy", detected_at=now - timedelta(hours=48)),
            _whale_event(self.ALPHA, "m2", "buy", detected_at=now - timedelta(hours=1)),
            _whale_event(self.BETA, "m2", "buy", detected_at=now - timedelta(hours=1)),
            _whale_event(self.ALPHA, "m6", "buy", detected_at=now - timedelta(hours=1)),
            _whale_event(self.BETA, "m6", "buy", detected_at=now - timedelta(hours=1)),
        ]
        clusters = clusters_from_events(events, window_hours=24, now=now)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(
            [bucket["market_id"] for bucket in clusters[0]["shared_markets"]], ["m2", "m6"]
        )

    def test_events_without_a_wallet_are_ignored(self):
        events = [{"market_id": "m1", "side": "buy"}] + self._clustered_events()
        self.assertEqual(
            len(clusters_from_events(events)),
            len(clusters_from_events(self._clustered_events())),
        )

    def test_clusters_are_sorted_by_size_then_wallet(self):
        events = [
            _whale_event(self.ALPHA, "m1", "buy"),
            _whale_event(self.BETA, "m1", "buy"),
            _whale_event(self.GAMMA, "m2", "buy"),
            _whale_event(self.DELTA, "m2", "buy"),
        ]
        clusters = clusters_from_events(events, min_co_occurrence=1)
        self.assertEqual([c["size"] for c in clusters], [2, 2])
        self.assertEqual(
            [c["wallets"][0] for c in clusters],
            sorted([c["wallets"][0] for c in clusters]),
        )
        self.assertEqual([c["cluster_id"] for c in clusters], ["c1", "c2"])


class WalletClusterServiceTests(unittest.TestCase):
    """``ctf_events_service.build_wallet_clusters`` over the whale_events table."""

    def setUp(self):
        from app.services.market_data import ctf_events_service as svc

        self.svc = svc
        svc._cache.clear()
        self.addCleanup(svc._cache.clear)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        self.engine.dispose()

    def _seed(self, wallet: str, market_id: str, side: str, tx_hash: str) -> None:
        self.db.add(
            WhaleEvent(
                wallet=wallet,
                market_id=market_id,
                token_id="123",
                side=side,
                size=30000.0,
                price=0.5,
                notional=15000.0,
                tx_hash=tx_hash,
                detected_at=utc_now(),
            )
        )
        self.db.commit()

    def test_clusters_are_built_from_the_events_table(self):
        alpha = "0x" + "11" * 20
        beta = "0x" + "22" * 20
        for market in ("m1", "m2"):
            for wallet in (alpha, beta):
                self._seed(wallet, market, "buy", f"0xtx{market}{wallet[:6]}")
        clusters = self.svc.build_wallet_clusters(24, 2, self.db)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["size"], 2)
        self.assertEqual(clusters[0]["co_occurrence"], 2)
        self.assertAlmostEqual(clusters[0]["total_notional"], 60000.0, places=3)

    def test_a_lone_wallet_forms_no_cluster(self):
        self._seed("0x" + "11" * 20, "m1", "buy", "0xtx0")
        self.assertEqual(self.svc.build_wallet_clusters(24, 2, self.db), [])

    def test_events_outside_the_window_are_ignored(self):
        alpha = "0x" + "11" * 20
        beta = "0x" + "22" * 20
        for market in ("m1", "m2"):
            for wallet in (alpha, beta):
                self._seed(wallet, market, "buy", f"0xtx{market}{wallet[:6]}")
        for event in self.db.query(WhaleEvent).all():
            event.detected_at = utc_now() - timedelta(hours=48)
        self.db.commit()
        self.assertEqual(self.svc.build_wallet_clusters(24, 2, self.db), [])


class NewRouteTests(unittest.TestCase):
    """Auth + happy path for both Phase B routes.

    The routers are mounted on a bare app rather than ``app.main``, as in
    ``test_venues.py``: these tests must not depend on the integration
    wiring. Auth stays real — a minted access token, no dependency
    override.
    """

    def setUp(self):
        from app.api.routes.markets.venues import router as venues_router
        from app.api.routes.markets.whales import router as whales_router

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.db.add(User(id=1, wallet_address=WALLET))
        self.db.commit()

        self.app = FastAPI()
        self.app.include_router(venues_router)
        self.app.include_router(whales_router)
        self.app.dependency_overrides[get_db] = self._db_override
        self.client = TestClient(self.app)
        self.addCleanup(self._cleanup)

        from app.services.market_data import ctf_events_service as svc

        self.svc = svc
        svc._cache.clear()
        self.addCleanup(svc._cache.clear)

    def _db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _cleanup(self):
        self.app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def test_mismatches_requires_a_token(self):
        self.assertEqual(self.client.get("/api/venues/mismatches").status_code, 401)

    def test_clusters_requires_a_token(self):
        self.assertEqual(self.client.get("/api/whales/clusters").status_code, 401)

    def test_mismatches_returns_signal_only_payload(self):
        self.db.add(
            VenueMarket(
                venue="kalshi",
                market_id="KX-1",
                question="Will the Fed cut rates in December?",
                prices_json='{"yes": 0.42, "no": 0.58}',
                liquidity=900.0,
            ),
        )
        self.db.add(
            VenueMarket(
                venue="predictit",
                market_id="18516",
                question="Will the Fed cut rates in December?",
                prices_json='{"yes": 0.55, "no": 0.45}',
                liquidity=100.0,
            ),
        )
        self.db.commit()
        response = self.client.get("/api/venues/mismatches", headers=_auth_headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["signal_only"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["mismatches"][0]["venues"], ["kalshi", "predictit"])
        self.assertTrue(body["mismatches"][0]["executable_signal"])

    def test_clusters_returns_an_empty_list_when_there_are_no_events(self):
        response = self.client.get("/api/whales/clusters", headers=_auth_headers())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["window_hours"], 24.0)
        self.assertEqual(body["min_co_occurrence"], 2)

    def test_clusters_reports_the_co_occurrence_grouping(self):
        alpha = "0x" + "11" * 20
        beta = "0x" + "22" * 20
        for market in ("m1", "m2"):
            for wallet in (alpha, beta):
                self.db.add(
                    WhaleEvent(
                        wallet=wallet,
                        market_id=market,
                        token_id="123",
                        side="buy",
                        size=30000.0,
                        price=0.5,
                        notional=15000.0,
                        tx_hash=f"0xtx{market}{wallet[:6]}",
                        detected_at=utc_now(),
                    )
                )
        self.db.commit()
        body = self.client.get("/api/whales/clusters", headers=_auth_headers()).json()
        self.assertEqual(body["count"], 1)
        cluster = body["clusters"][0]
        self.assertEqual(cluster["wallets"], sorted([alpha, beta]))
        self.assertEqual(cluster["co_occurrence"], 2)
        self.assertEqual(cluster["shared_markets"][0]["market_id"], "m1")
        self.assertAlmostEqual(cluster["total_notional"], 60000.0, places=3)

    def test_clusters_rejects_a_zero_window(self):
        response = self.client.get(
            "/api/whales/clusters", params={"window_hours": 0}, headers=_auth_headers()
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
