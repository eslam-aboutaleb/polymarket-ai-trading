"""Venue adapter tests (plan 05 Phase A).

Covers the four things Phase A promises, in dependency order:

* **Pure parser** — ``parse_kalshi_markets`` runs on a fixture recorded
  from the live Kalshi ``trade-api/v2/markets`` endpoint with zero
  network calls, and asserts the normalized shape (prices, liquidity
  proxy, url, real-money flag, content hash).
* **Graceful degradation** — a 404, a timeout, a malformed payload and
  a transport exception all return ``[]`` and never raise, and a failure
  is not cached.
* **Registry** — registration is idempotent and thread-safe.
* **Cache + routes** — the model round-trips through
  ``Base.metadata.create_all``, the refresh loop writes it, the routes
  reject an unauthenticated caller with 401 and answer a minted token,
  and the AI-service token path on the cross route works.
* **MCP tool** — ``cross_venue_markets`` returns the backend's groups,
  and degrades to ``[]`` exactly like ``binance_skills._get``.

No test touches the network: Kalshi traffic goes through
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.base import Base
from app.models.user import User
from app.models.venue_market import VenueMarket
from app.schemas.market import NormalizedMarket
from app.security.auth import create_access_token
from app.services.venues import base as venues_base
from app.services.venues.kalshi import KalshiAdapter, parse_kalshi_markets
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20
SERVICE_TOKEN = "venue-service-token"

# Recorded from GET https://api.elections.kalshi.com/trade-api/v2/markets
# ?limit=3&status=open&series_ticker=KXHIGHNY on 2026-10-11. Dollar-string
# prices, one illiquid tail market (T76: no yes bid) and two quoted ones.
RECORDED_KALSHI_MARKETS = [
    {
        "ticker": "KXHIGHNY-26OCT12-T76",
        "title": "Will the maximum temperature be >76\u00b0 on Oct 12, 2026?",
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "0.0100",
        "no_bid_dollars": "0.9900",
        "no_ask_dollars": "1.0000",
        "last_price_dollars": "0.0100",
        "volume_24h_fp": "737.00",
        "open_interest_fp": "719.00",
        "close_time": "2026-10-13T05:00:00Z",
        "status": "active",
        "market_type": "binary",
        "event_ticker": "KXHIGHNY-26OCT12",
    },
    {
        "ticker": "KXHIGHNY-26OCT12-T69",
        "title": "Will the maximum temperature be <69\u00b0 on Oct 12, 2026?",
        "yes_bid_dollars": "0.0700",
        "yes_ask_dollars": "0.0800",
        "no_bid_dollars": "0.9200",
        "no_ask_dollars": "0.9300",
        "last_price_dollars": "0.0800",
        "volume_24h_fp": "652.42",
        "open_interest_fp": "649.42",
        "close_time": "2026-10-13T05:00:00Z",
        "status": "active",
        "market_type": "binary",
        "event_ticker": "KXHIGHNY-26OCT12",
    },
    {
        "ticker": "KXHIGHNY-26OCT12-B75.5",
        "title": "Will the maximum temperature be 75-76\u00b0 on Oct 12, 2026?",
        "yes_bid_dollars": "0.0300",
        "yes_ask_dollars": "0.0400",
        "no_bid_dollars": "0.9600",
        "no_ask_dollars": "0.9700",
        "last_price_dollars": "0.0400",
        "volume_24h_fp": "191.30",
        "open_interest_fp": "191.30",
        "close_time": "2026-10-13T05:00:00Z",
        "status": "active",
        "market_type": "binary",
        "event_ticker": "KXHIGHNY-26OCT12",
    },
]


def _auth_headers() -> dict:
    token = create_access_token({"user_id": 1, "sub": WALLET, "is_admin": False})
    return {"Authorization": f"Bearer {token}"}


def _run_async(coro):
    """Run one coroutine to completion from sync test code."""
    return asyncio.run(coro)


def _patch_client(handler):
    """Point the kalshi module's ``httpx.AsyncClient`` at a MockTransport.

    The real class is captured before the patch so the factory itself
    does not recurse into the patched name.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("headers", None)
        return real_client(transport=transport, timeout=15.0)

    patcher = patch("app.services.venues.kalshi.httpx.AsyncClient", factory)
    patcher.start()
    return patcher


class RecordedKalshiParserTests(unittest.TestCase):
    """The pure parser against the recorded fixture — zero network calls."""

    def setUp(self):
        self.markets = parse_kalshi_markets(RECORDED_KALSHI_MARKETS)

    def test_every_recorded_market_parses(self):
        self.assertEqual(len(self.markets), len(RECORDED_KALSHI_MARKETS))
        self.assertEqual(
            [m.market_id for m in self.markets],
            ["KXHIGHNY-26OCT12-T76", "KXHIGHNY-26OCT12-T69", "KXHIGHNY-26OCT12-B75.5"],
        )
        for market in self.markets:
            self.assertEqual(market.venue, "kalshi")
            self.assertEqual(market.outcomes, ("Yes", "No"))
            self.assertTrue(market.is_real_money)

    def test_prices_come_from_the_book_midpoint(self):
        t69 = self.markets[1]
        self.assertAlmostEqual(t69.yes_price, 0.075)
        self.assertAlmostEqual(t69.no_price, 0.925)
        band = self.markets[2]
        self.assertAlmostEqual(band.yes_price, 0.035)
        self.assertAlmostEqual(band.no_price, 0.965)

    def test_onesided_book_falls_back_to_last_trade(self):
        tail = self.markets[0]
        self.assertAlmostEqual(tail.yes_price, 0.01)
        self.assertAlmostEqual(tail.no_price, 0.995)

    def test_volume_and_liquidity_proxy(self):
        t69 = self.markets[1]
        self.assertAlmostEqual(t69.volume_24h, 652.42)
        # Kalshi publishes no depth: open interest is the proxy.
        self.assertAlmostEqual(t69.liquidity, 649.42)

    def test_end_date_url_and_hash(self):
        t69 = self.markets[1]
        self.assertIsNotNone(t69.end_date)
        self.assertEqual(t69.end_date.isoformat(), "2026-10-13T05:00:00+00:00")
        self.assertEqual(t69.url, "https://kalshi.com/markets/KXHIGHNY-26OCT12-T69")
        self.assertTrue(t69.raw_hash)
        # The hash is content-derived: stable per payload, different on change.
        self.assertEqual(t69.raw_hash, self.markets[1].raw_hash)
        changed = dict(RECORDED_KALSHI_MARKETS[1], last_price_dollars="0.0810")
        self.assertNotEqual(parse_kalshi_markets([changed])[0].raw_hash, t69.raw_hash)

    def test_to_dict_is_json_ready(self):
        payload = self.markets[1].to_dict()
        self.assertEqual(
            sorted(payload),
            sorted(
                [
                    "venue",
                    "market_id",
                    "question",
                    "outcomes",
                    "yes_price",
                    "no_price",
                    "volume_24h",
                    "liquidity",
                    "end_date",
                    "url",
                    "is_real_money",
                    "raw_hash",
                ]
            ),
        )
        json.dumps(payload)
        self.assertEqual(payload["end_date"], "2026-10-13T05:00:00+00:00")

    def test_malformed_rows_are_skipped_not_fatal(self):
        raw = [
            "not-a-dict",
            {"title": "no ticker", "status": "active"},
            {"ticker": "KXHIGHNY-26OCT12-CLOSED", "status": "closed", "title": "settled"},
            {"ticker": "KXHIGHNY-26OCT12-T69", "title": "dup"},
        ]
        markets = parse_kalshi_markets(raw)
        self.assertEqual([m.market_id for m in markets], ["KXHIGHNY-26OCT12-T69"])

    def test_missing_numeric_fields_default_to_zero(self):
        market = parse_kalshi_markets([{"ticker": "KX-NO-NUMBERS", "status": "active"}])[0]
        self.assertEqual(market.volume_24h, 0.0)
        self.assertEqual(market.liquidity, 0.0)
        self.assertIsNone(market.yes_price)
        self.assertIsNone(market.no_price)
        self.assertIsNone(market.end_date)
        self.assertEqual(market.question, "")

    def test_adapter_delegates_to_the_pure_function(self):
        adapter = KalshiAdapter()
        self.assertEqual(adapter.name, "kalshi")
        self.assertEqual(
            [m.market_id for m in adapter.parse_markets(RECORDED_KALSHI_MARKETS)],
            [m.market_id for m in self.markets],
        )


class KalshiFetchDegradationTests(unittest.TestCase):
    """Every failure mode returns ``[]`` and never raises."""

    def setUp(self):
        from app.services.venues import kalshi as kalshi_module

        self.kalshi = kalshi_module
        self.addCleanup(self.kalshi.CACHE.clear)
        self.kalshi.CACHE.clear()

    def _fetch(self, handler) -> list[dict]:
        patcher = _patch_client(handler)
        self.addCleanup(patcher.stop)
        return _run_async(KalshiAdapter().fetch_markets(limit=10))

    def test_404_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        self.assertEqual(self._fetch(handler), [])

    def test_timeout_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        self.assertEqual(self._fetch(handler), [])

    def test_transport_error_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        self.assertEqual(self._fetch(handler), [])

    def test_payload_without_markets_list_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"cursor": "abc"})

        self.assertEqual(self._fetch(handler), [])

    def test_successful_payload_is_parsed_by_the_caller(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/markets", str(request.url))
            self.assertIn("status=open", str(request.url))
            return httpx.Response(200, json={"markets": RECORDED_KALSHI_MARKETS})

        raw = self._fetch(handler)
        self.assertEqual(len(raw), 3)
        self.assertEqual(raw[1]["ticker"], "KXHIGHNY-26OCT12-T69")

    def test_second_fetch_is_served_from_cache(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, json={"markets": RECORDED_KALSHI_MARKETS})

        patcher = _patch_client(handler)
        self.addCleanup(patcher.stop)
        adapter = KalshiAdapter()
        first = _run_async(adapter.fetch_markets(limit=10))
        second = _run_async(adapter.fetch_markets(limit=10))
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertEqual(calls["count"], 1)

    def test_failure_is_not_cached(self):
        with patch.object(
            KalshiAdapter,
            "_get_markets",
            new=AsyncMock(return_value=None),
        ):
            first = _run_async(KalshiAdapter().fetch_markets(limit=10))
        self.assertEqual(first, [])
        self.assertIsNone(self.kalshi.CACHE.get("markets:10"))


class VenueRegistryTests(unittest.TestCase):
    """Registration is idempotent and safe under concurrent writers."""

    def setUp(self):
        self.registry = venues_base.VenueRegistry()

    def test_register_get_and_all(self):
        kalshi = KalshiAdapter()
        other = _FakeAdapter("other")
        self.registry.register("kalshi", kalshi)
        self.registry.register("other", other)
        self.assertEqual(self.registry.names(), ["kalshi", "other"])
        self.assertIs(self.registry.get("kalshi"), kalshi)
        self.assertEqual([a.name for a in self.registry.all()], ["kalshi", "other"])
        self.assertIsNone(self.registry.get("missing"))

    def test_registration_is_idempotent(self):
        self.registry.register("kalshi", KalshiAdapter())
        replacement = KalshiAdapter()
        self.registry.register("kalshi", replacement)
        self.assertEqual(self.registry.names(), ["kalshi"])
        self.assertIs(self.registry.get("kalshi"), replacement)
        self.assertEqual(len(self.registry.all()), 1)

    def test_concurrent_registration_keeps_one_entry_per_name(self):
        import threading

        errors: list[Exception] = []

        def register(index: int) -> None:
            try:
                for _ in range(20):
                    self.registry.register(f"venue-{index}", _FakeAdapter(f"venue-{index}"))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.registry.names()), 8)
        self.assertEqual(len(self.registry.all()), 8)

    def test_protocol_is_runtime_checkable(self):
        self.assertIsInstance(KalshiAdapter(), venues_base.VenueAdapter)


class _FakeAdapter:
    def __init__(self, name: str):
        self.name = name

    async def fetch_markets(self, *, limit: int = 100) -> list[dict]:
        return []

    def parse_markets(self, raw: list[dict]) -> list[NormalizedMarket]:
        return []


class VenueMarketModelTests(unittest.TestCase):
    """The cache table round-trips through ``Base.metadata.create_all``."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        # Session closes before the engine disposes, or SQLite reports
        # "cannot operate on a closed database" on the final rollback.
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        self.engine.dispose()

    def _row(self, **kwargs) -> VenueMarket:
        payload = {
            "venue": kwargs.get("venue", "kalshi"),
            "market_id": kwargs.get("market_id", "KXHIGHNY-26OCT12-T69"),
            "question": kwargs.get("question", "Will the maximum temperature be <69?"),
            "prices_json": json.dumps({"yes": 0.075, "no": 0.925, "url": "https://kalshi.com/x"}),
            "volume_24h": 652.42,
            "liquidity": 649.42,
        }
        payload.update({k: v for k, v in kwargs.items() if k in payload})
        return VenueMarket(**payload)

    def test_row_round_trip(self):
        self.db.add(self._row())
        self.db.commit()
        row = self.db.query(VenueMarket).one()
        self.assertEqual(row.venue, "kalshi")
        self.assertEqual(row.question, "Will the maximum temperature be <69?")
        self.assertAlmostEqual(row.volume_24h, 652.42)
        self.assertIsNotNone(row.fetched_at)
        self.assertIsNotNone(row.created_at)
        self.assertIn("KXHIGHNY", repr(row))

    def test_duplicate_venue_market_is_rejected(self):
        from sqlalchemy.exc import IntegrityError

        self.db.add(self._row())
        self.db.commit()
        self.db.add(self._row())
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()


class VenueMarketMigrationTests(unittest.TestCase):
    """The hand-written 20261011_0016 migration, run in-process.

    Same idiom as ``test_accounts.AccountMigrationTests``: the table is
    dropped from a schema built by ``Base.metadata.create_all`` so the
    migration's own SQL runs, and the assertions are on the resulting
    table shape rather than on the model.
    """

    MIGRATION_MODULE = "migrations.versions.20261011_0016_add_venue_markets"

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        with self.engine.connect() as conn:
            conn.execute(text("DROP TABLE venue_markets"))
            conn.commit()
        self.addCleanup(self.engine.dispose)

    def _run(self, direction: str) -> None:
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        with self.engine.connect() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(migration_context=context):
                getattr(importlib.import_module(self.MIGRATION_MODULE), direction)()
            conn.commit()

    def _table_info(self) -> dict[str, dict]:
        with self.engine.connect() as conn:
            columns = {
                row[1]: row for row in conn.execute(text("PRAGMA table_info(venue_markets)"))
            }
            indexes = {
                row[1]: row for row in conn.execute(text("PRAGMA index_list(venue_markets)"))
            }
        return {"columns": columns, "indexes": indexes}

    def test_upgrade_creates_the_table_with_its_constraints(self):
        self._run("upgrade")
        info = self._table_info()
        self.assertEqual(
            {
                "venue",
                "market_id",
                "question",
                "prices_json",
                "volume_24h",
                "liquidity",
                "end_date",
                "fetched_at",
                "created_at",
                "updated_at",
            },
            set(info["columns"]) - {"id"},
        )
        # ``origin`` "u" marks the unique constraint, "c" a plain index.
        self.assertTrue(
            any(row[3] == "u" and row[2] == 1 for row in info["indexes"].values()),
            info["indexes"],
        )
        self.assertIn("ix_venue_markets_venue_fetched", info["indexes"])

    def test_upgrade_is_idempotent(self):
        self._run("upgrade")
        self._run("upgrade")
        self.assertEqual(len(self._table_info()["columns"]), 11)

    def test_downgrade_drops_the_table(self):
        self._run("upgrade")
        self._run("downgrade")
        with self.engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            }
        self.assertNotIn("venue_markets", tables)

    def test_downgrade_on_a_missing_table_is_a_noop(self):
        self._run("downgrade")  # never created — must not raise


class VenueServiceTests(unittest.TestCase):
    """The refresh writer, the feature flags and the cross-venue grouping."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.session_factory()

        from app.services import venue_market_service as service

        self.service = service
        # A fresh registry per class: the module-level bootstrap already
        # registered kalshi, and these tests must not call the real venue.
        self.registry = venues_base.VenueRegistry()
        registry_patcher = patch.object(service, "registry", self.registry)
        registry_patcher.start()

        settings = MagicMock()
        settings.venue_markets_refresh_seconds = 300
        settings.venue_kalshi_enabled = True
        settings.venue_fake_enabled = True
        settings.venue_broken_enabled = True
        settings.venue_empty_enabled = True
        settings_patcher = patch.object(service, "get_settings", return_value=settings)
        settings_patcher.start()

        self.addCleanup(self._cleanup)
        self.addCleanup(settings_patcher.stop)
        self.addCleanup(registry_patcher.stop)
        self.addCleanup(service._adapter_states.clear)

    def _cleanup(self):
        self.db.close()
        self.engine.dispose()

    def _seed(self, venue: str, question: str, yes: float, liquidity: float) -> VenueMarket:
        row = VenueMarket(
            venue=venue,
            market_id=f"{venue}-{question[:8]}",
            question=question,
            prices_json=json.dumps({"yes": yes, "no": 1 - yes}),
            volume_24h=10.0,
            liquidity=liquidity,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_refresh_writes_rows_for_an_enabled_venue(self):
        self.registry.register("fake", _FakeFetchAdapter("fake"))
        summary = _run_async(self.service.refresh_venue_markets(self.db))
        self.assertEqual(summary["venues"], 1)
        self.assertEqual(summary["rows"], 2)
        self.assertEqual(summary["errors"], 0)
        rows = self.db.query(VenueMarket).filter(VenueMarket.venue == "fake").all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.service._adapter_states["fake"]["markets"], 2)
        self.assertIsNone(self.service._adapter_states["fake"]["last_error"])

    def test_refresh_skips_a_venue_whose_flag_is_off(self):
        self.registry.register("fake", _FakeFetchAdapter("fake"))
        settings = MagicMock()
        settings.venue_markets_refresh_seconds = 300
        settings.venue_fake_enabled = False
        with patch.object(self.service, "get_settings", return_value=settings):
            summary = _run_async(self.service.refresh_venue_markets(self.db))
        self.assertEqual(summary["venues"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(self.db.query(VenueMarket).count(), 0)

    def test_refresh_isolates_a_failing_venue(self):
        self.registry.register("broken", _BrokenAdapter())
        summary = _run_async(self.service.refresh_venue_markets(self.db))
        self.assertEqual(summary["errors"], 1)
        self.assertIn("boom", self.service._adapter_states["broken"]["last_error"])
        self.assertEqual(self.db.query(VenueMarket).count(), 0)

    def test_refresh_upserts_and_prunes(self):
        self.registry.register("fake", _FakeFetchAdapter("fake"))
        _run_async(self.service.refresh_venue_markets(self.db))
        stale = VenueMarket(venue="fake", market_id="FAKE-GONE", question="closed market")
        self.db.add(stale)
        self.db.commit()
        _run_async(self.service.refresh_venue_markets(self.db))
        ids = {row.market_id for row in self.db.query(VenueMarket).all()}
        self.assertNotIn("FAKE-GONE", ids)
        self.assertEqual(len(ids), 2)

    def test_empty_fetch_prunes_nothing(self):
        self.registry.register("empty", _FakeFetchAdapter("empty", markets=[]))
        self._seed("empty", "still here", 0.5, 1.0)
        _run_async(self.service.refresh_venue_markets(self.db))
        self.assertEqual(self.db.query(VenueMarket).count(), 1)

    def test_cross_venue_groups_only_multiple_venues(self):
        self._seed("kalshi", "will it rain tomorrow", 0.30, 100.0)
        self._seed("other", "will it rain tomorrow", 0.45, 50.0)
        self._seed("other", "something else entirely", 0.10, 999.0)
        groups = self.service.get_cross_venue_groups(self.db, "rain")
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["venues"], ["kalshi", "other"])
        self.assertEqual(group["questions"], group["questions"])
        self.assertAlmostEqual(group["yes_min"], 0.30)
        self.assertAlmostEqual(group["yes_max"], 0.45)
        self.assertAlmostEqual(group["edge"], 0.15, places=4)

    def test_normalize_question_is_order_independent(self):
        self.assertEqual(
            self.service.normalize_question("Will it RAIN, tomorrow?"),
            self.service.normalize_question("tomorrow rain it will"),
        )

    def test_search_term_wildcards_match_literally(self):
        self._seed("kalshi", "100% chance of rain", 0.5, 10.0)
        self.assertEqual(len(self.service.get_venue_markets(self.db, q="100%")), 1)
        # A trailing wildcard in the query must not widen the match.
        self.assertEqual(self.service.get_venue_markets(self.db, q="rain%"), [])

    def test_adapter_health_reports_flag_and_last_fetch(self):
        self.registry.register("fake", _FakeFetchAdapter("fake"))
        _run_async(self.service.refresh_venue_markets(self.db))
        health = {row["name"]: row for row in self.service.get_adapter_health()}
        self.assertIn("fake", health)
        self.assertTrue(health["fake"]["enabled"])
        self.assertEqual(health["fake"]["markets"], 2)
        self.assertIsNone(health["fake"]["last_error"])
        metrics = self.service.get_venue_market_metrics()
        self.assertEqual(metrics["scheduler_name"], "venue_markets")
        self.assertEqual(metrics["interval_seconds"], 300)
        self.assertEqual(metrics["refreshes"], 1)
        self.assertEqual(metrics["rows_written"], 2)

    def test_kalshi_is_bootstrapped_into_the_module_registry(self):
        # Importing the service registers the reference adapter exactly once.
        self.assertIn("kalshi", venues_base.registry.names())
        self.assertIsInstance(venues_base.registry.get("kalshi"), KalshiAdapter)


class _FakeFetchAdapter:
    def __init__(self, name: str, markets: list[NormalizedMarket] | None = None):
        self.name = name
        self.markets = (
            markets
            if markets is not None
            else [
                NormalizedMarket(
                    venue=name,
                    market_id=f"{name}-1",
                    question="fake market one",
                    yes_price=0.4,
                    no_price=0.6,
                    volume_24h=1.0,
                    liquidity=2.0,
                ),
                NormalizedMarket(
                    venue=name,
                    market_id=f"{name}-2",
                    question="fake market two",
                    yes_price=0.7,
                    no_price=0.3,
                    volume_24h=3.0,
                    liquidity=4.0,
                ),
            ]
        )

    async def fetch_markets(self, *, limit: int = 100) -> list[dict]:
        return [market.to_dict() for market in self.markets]

    def parse_markets(self, raw: list[dict]) -> list[NormalizedMarket]:
        return list(self.markets)


class _BrokenAdapter:
    name = "broken"

    async def fetch_markets(self, *, limit: int = 100) -> list[dict]:
        raise RuntimeError("boom")

    def parse_markets(self, raw: list[dict]) -> list[NormalizedMarket]:
        return []


class VenueRouteTests(unittest.TestCase):
    """Happy path + auth path for every venue route.

    The router is mounted on a bare app rather than ``app.main``: the
    routes are registered in ``main.py`` by the integration step, and
    these tests must not depend on that wiring. Auth stays real — a
    minted access token, no dependency override.
    """

    def setUp(self):
        from fastapi import FastAPI

        from app.api.routes.venues import router as venues_router

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
        self.app.dependency_overrides[get_db] = self._db_override
        self.client = TestClient(self.app)
        self.addCleanup(self._cleanup)

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

    def _seed(self, venue: str, question: str, yes: float, liquidity: float) -> None:
        self.db.add(
            VenueMarket(
                venue=venue,
                market_id=f"{venue}-{question[:6]}",
                question=question,
                prices_json=json.dumps({"yes": yes, "no": 1 - yes, "url": "https://x"}),
                volume_24h=5.0,
                liquidity=liquidity,
            )
        )
        self.db.commit()

    def test_markets_requires_a_token(self):
        self.assertEqual(self.client.get("/api/venues/markets").status_code, 401)

    def test_adapters_requires_a_token(self):
        self.assertEqual(self.client.get("/api/venues/adapters").status_code, 401)

    def test_cross_markets_requires_a_token(self):
        response = self.client.get("/api/venues/markets/cross", params={"q": "rain"})
        self.assertEqual(response.status_code, 401)

    def test_markets_returns_rows_for_an_authenticated_user(self):
        self._seed("kalshi", "will it rain tomorrow", 0.3, 100.0)
        response = self.client.get(
            "/api/venues/markets",
            params={"venue": "kalshi"},
            headers=_auth_headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["markets"][0]["venue"], "kalshi")
        self.assertAlmostEqual(body["markets"][0]["yes_price"], 0.3)

    def test_markets_filter_by_question(self):
        self._seed("kalshi", "will it rain tomorrow", 0.3, 100.0)
        self._seed("kalshi", "will the sun shine", 0.9, 50.0)
        body = self.client.get(
            "/api/venues/markets",
            params={"q": "rain"},
            headers=_auth_headers(),
        ).json()
        self.assertEqual(body["total"], 1)
        self.assertIn("rain", body["markets"][0]["question"])

    def test_adapters_reports_registered_health(self):
        body = self.client.get("/api/venues/adapters", headers=_auth_headers()).json()
        names = [row["name"] for row in body["adapters"]]
        self.assertIn("kalshi", names)
        self.assertEqual(body["scheduler"]["scheduler_name"], "venue_markets")

    def test_cross_markets_groups_across_venues(self):
        self._seed("kalshi", "will it rain tomorrow", 0.3, 100.0)
        self._seed("other", "will it rain tomorrow", 0.45, 50.0)
        body = self.client.get(
            "/api/venues/markets/cross",
            params={"q": "rain"},
            headers=_auth_headers(),
        ).json()
        self.assertEqual(len(body["groups"]), 1)
        group = body["groups"][0]
        self.assertEqual(group["venues"], ["kalshi", "other"])
        self.assertAlmostEqual(group["edge"], 0.15, places=4)
        self.assertEqual(len(group["rows"]), 2)

    def test_cross_markets_accepts_the_service_token(self):
        self._seed("kalshi", "will it rain tomorrow", 0.3, 100.0)
        settings = MagicMock()
        settings.venue_api_token = SERVICE_TOKEN
        with patch("app.api.routes.venues.get_settings", return_value=settings):
            ok = self.client.get(
                "/api/venues/markets/cross",
                params={"q": "rain"},
                headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            )
            wrong = self.client.get(
                "/api/venues/markets/cross",
                params={"q": "rain"},
                headers={"Authorization": "Bearer not-the-token"},
            )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.json()["groups"]), 0)  # one venue only
        self.assertEqual(wrong.status_code, 401)


class CrossVenueMcpToolTests(unittest.TestCase):
    """The research MCP tool reads the backend's cached groups."""

    @classmethod
    def setUpClass(cls):
        import sys

        # ``ddgs`` is an optional runtime dep the backend venv lacks.
        if "ddgs" not in sys.modules:
            module = ModuleType("ddgs")
            module.DDGS = MagicMock()
            sys.modules["ddgs"] = module
        server_file = (
            Path(__file__).resolve().parents[2] / "services" / "mcps" / "research" / "server.py"
        )
        spec = importlib.util.spec_from_file_location("research_server_venues", server_file)
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def _call_tool(self, tool: str, args: dict) -> dict:
        async def _go():
            return await asyncio.to_thread(self.server._run_one, {"tool": tool, "args": args})

        return _run_async(_go())

    def _patch_transport(self, handler):
        real_client = httpx.AsyncClient
        patcher = patch.object(
            self.server.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_groups_come_back_from_the_backend(self):
        seen: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["request"] = request
            return httpx.Response(
                200,
                json={"query": "rain", "groups": [{"key": "rain", "venues": ["kalshi"]}]},
            )

        self._patch_transport(handler)
        with (
            patch.object(self.server, "VENUE_API_BASE", "http://backend:8002"),
            patch.object(self.server, "VENUE_API_TOKEN", SERVICE_TOKEN),
        ):
            result = self._call_tool("cross_venue_markets", {"query": "rain", "limit": 5})
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["result"], [{"key": "rain", "venues": ["kalshi"]}])
        self.assertIn("/api/venues/markets/cross", str(seen["request"].url))
        self.assertIn("q=rain", str(seen["request"].url))

    def test_backend_error_degrades_to_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, json={"detail": "bad gateway"})

        self._patch_transport(handler)
        result = self._call_tool("cross_venue_markets", {"query": "rain"})
        self.assertEqual(result, {"ok": True, "result": []})

    def test_transport_exception_degrades_to_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("backend unreachable")

        self._patch_transport(handler)
        result = self._call_tool("cross_venue_markets", {"query": "rain"})
        self.assertEqual(result, {"ok": True, "result": []})

    def test_empty_query_short_circuits(self):
        result = self._call_tool("cross_venue_markets", {"query": "   "})
        self.assertEqual(result, {"ok": True, "result": []})

    def test_unsupported_tool_still_errors(self):
        result = self._call_tool("not_a_tool", {})
        self.assertEqual(result["ok"], False)


if __name__ == "__main__":
    unittest.main()
