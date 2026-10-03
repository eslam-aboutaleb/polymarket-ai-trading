"""Additional coverage tests for :mod:`app.services.market_maker_service`.

Complements ``tests/test_market_maker_service.py`` (which owns the
strategy maths, the CLOB I/O helpers and the live sync cycle).
This file targets the remaining statements:

* ``_fetch_order_book`` — the depth-aware order-book fetch:
  best bid/ask, midpoint, per-side USDC depth and the
  ``min``-combined depth, plus the non-200 / empty-book /
  transport-error paths and the missing-size-or-price
  defaults;
* the paper (simulation) sync path — ``_sync_market_maker``
  with the owning user's ``simulation_mode`` flag set: the
  order-book fetch, the ``no book`` error, the delegation to
  ``_sync_market_maker_paper`` and the fact that no wallet
  credentials are required;
* ``_sync_market_maker_paper`` — simulated fills recorded as
  ``UserTrade`` rows with ``status="simulated"``, the
  persisted counters and the summary payload;
* ``_maker_loop`` — the advisory-lock acquisition guard, the
  per-cycle heartbeat, the lock release on shutdown and the
  graceful-shutdown cleanup;
* ``stop_all_market_makers`` — the shutdown broadcast log.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.market_maker_service as market_maker_service
from app.services.market_maker_service import MAX_BANDS

CLOB_API = market_maker_service.POLYMARKET_CLOB_API


# ─────────────── config doubles ───────────────


def _config(**overrides) -> SimpleNamespace:
    """A MarketMakerConfig stand-in with every field the sync reads."""
    base = {
        "id": 1,
        "user_id": 7,
        "condition_id": "cond-1",
        "token_id_yes": "token-yes",
        "token_id_no": "token-no",
        "strategy": "bands",
        "enabled": True,
        "num_bands": 3,
        "min_spread": 0.02,
        "max_spread": 0.10,
        "band_order_size": 10.0,
        "amm_liquidity": 1000.0,
        "max_collateral": 1000.0,
        "sync_interval_seconds": 30,
        "min_order_size": 0.1,
        "min_price": 0.01,
        "max_price": 0.99,
        "status": "idle",
        "last_sync_at": None,
        "last_error": None,
        "total_orders_placed": 0,
        "total_orders_cancelled": 0,
        "total_volume_usdc": 0.0,
        "current_open_orders": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(user_id: int = 7, wallet: str = "0xwallet") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, wallet_address=wallet)


def _session(first_results=(), all_results=()) -> MagicMock:
    """A session double whose ``.first()``/``.all()`` results are scripted."""
    db = MagicMock(name="db")
    query = MagicMock(name="query")
    db.query.return_value = query
    if first_results:
        results = list(first_results)

        def _first():
            if not results:
                return None
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        query.filter.return_value.first.side_effect = _first
    else:
        query.filter.return_value.first.return_value = None
    if all_results is not None:
        query.filter.return_value.all.return_value = list(all_results)
    else:
        query.filter.return_value.all.return_value = []
    return db


# ─────────────── httpx doubles ───────────────


class _FakeAsyncResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient``; records constructor + GET args."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.init_kwargs: dict = {}
        self.get_calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, url, params=None):
        self.get_calls.append((url, params))
        if self.error is not None:
            raise self.error
        return self.response


def _async_client_patch(client: _FakeAsyncClient):
    def _factory(**kwargs):
        client.init_kwargs = kwargs
        return client

    return patch("httpx.AsyncClient", _factory)


# ─────────────── order book fetching ───────────────


class FetchOrderBookTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_book_with_per_side_depths(self):
        client = _FakeAsyncClient(
            _FakeAsyncResponse(
                payload={
                    "bids": [
                        {"price": "0.4", "size": "10"},
                        {"price": "0.3", "size": "5"},
                    ],
                    "asks": [{"price": "0.6", "size": "2"}],
                }
            )
        )

        with _async_client_patch(client):
            book = await market_maker_service._fetch_order_book("token-yes")

        self.assertEqual(
            book,
            {
                "best_bid": 0.4,
                "best_ask": 0.6,
                "midpoint": 0.5,
                "bid_depth": 5.5,  # 10×0.4 + 5×0.3
                "ask_depth": 1.2,  # 2×0.6
                "depth": 1.2,  # min(bid_depth, ask_depth)
            },
        )
        self.assertEqual(client.get_calls, [(f"{CLOB_API}/book", {"token_id": "token-yes"})])
        self.assertEqual(client.init_kwargs, {"timeout": 10})

    async def test_depth_is_the_thinner_side(self):
        client = _FakeAsyncClient(
            _FakeAsyncResponse(
                payload={
                    "bids": [{"price": "0.4", "size": "100"}],
                    "asks": [{"price": "0.6", "size": "1"}],
                }
            )
        )

        with _async_client_patch(client):
            book = await market_maker_service._fetch_order_book("token-yes")

        self.assertEqual(book["bid_depth"], 40.0)
        self.assertEqual(book["ask_depth"], 0.6)
        self.assertEqual(book["depth"], 0.6)

    async def test_non_200_returns_none(self):
        client = _FakeAsyncClient(_FakeAsyncResponse(status_code=503))

        with _async_client_patch(client):
            self.assertIsNone(await market_maker_service._fetch_order_book("token-yes"))

    async def test_missing_or_empty_sides_return_none(self):
        for payload in ({}, {"bids": []}, {"asks": []}, {"bids": [{"price": 0.4}]}):
            with self.subTest(payload=payload):
                client = _FakeAsyncClient(_FakeAsyncResponse(payload=payload))
                with _async_client_patch(client):
                    self.assertIsNone(await market_maker_service._fetch_order_book("token-yes"))

    async def test_missing_size_or_price_defaults_to_zero(self):
        client = _FakeAsyncClient(
            _FakeAsyncResponse(
                payload={
                    # First level of each side carries the price;
                    # deeper levels miss size or price entirely.
                    "bids": [{"price": "0.4"}, {"size": "5"}],
                    "asks": [{"price": "0.6"}, {"size": "2"}],
                }
            )
        )

        with _async_client_patch(client):
            book = await market_maker_service._fetch_order_book("token-yes")

        self.assertEqual(book["best_bid"], 0.4)
        self.assertEqual(book["best_ask"], 0.6)
        self.assertEqual(book["midpoint"], 0.5)
        self.assertEqual(book["bid_depth"], 0.0)
        self.assertEqual(book["ask_depth"], 0.0)
        self.assertEqual(book["depth"], 0.0)

    async def test_transport_error_returns_none(self):
        client = _FakeAsyncClient(error=RuntimeError("connection reset"))

        with _async_client_patch(client):
            self.assertIsNone(await market_maker_service._fetch_order_book("token-yes"))


# ─────────────── paper (simulation) sync ───────────────


class PaperSyncCycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fetch_order_book = AsyncMock()
        self.fetch_midpoint = AsyncMock(return_value=0.5)
        self.paper_sync = AsyncMock(return_value={"simulated": True})
        self.credentials = MagicMock(return_value={"private_key": "0xpk"})
        for name, mock in (
            ("_fetch_order_book", self.fetch_order_book),
            ("_fetch_midpoint", self.fetch_midpoint),
            ("_sync_market_maker_paper", self.paper_sync),
            ("load_wallet_credentials", self.credentials),
        ):
            patcher = patch.object(market_maker_service, name, new=mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _run(db):
        return patch.object(market_maker_service, "SessionLocal", return_value=db)

    async def test_paper_mode_reads_the_book_and_delegates(self):
        config = _config(num_bands=1)
        settings = SimpleNamespace(simulation_mode=True)
        db = _session([config, _user(), settings])
        book = {
            "best_bid": 0.48,
            "best_ask": 0.52,
            "midpoint": 0.5,
            "bid_depth": 10.0,
            "ask_depth": 10.0,
            "depth": 10.0,
        }
        self.fetch_order_book.return_value = book
        expected = market_maker_service._compute_bands_orders(0.5, config)

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        # Paper mode never touches credentials or the live midpoint.
        self.credentials.assert_not_called()
        self.fetch_midpoint.assert_not_awaited()
        self.fetch_order_book.assert_awaited_once_with("token-yes")
        self.paper_sync.assert_awaited_once_with(db, config, 0.5, expected, book)
        self.assertEqual(result, {"simulated": True})

    async def test_paper_mode_without_a_book_reports_the_error(self):
        config = _config()
        settings = SimpleNamespace(simulation_mode=True)
        db = _session([config, _user(), settings])
        self.fetch_order_book.return_value = None

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.assertEqual(result, {"error": "no book"})
        self.assertEqual(config.last_error, "Could not fetch order book")
        db.commit.assert_called_once_with()
        self.paper_sync.assert_not_awaited()

    async def test_missing_simulation_flag_defaults_to_live_mode(self):
        # A UserSettings row without the attribute is treated as
        # live mode (getattr default), so credentials are required.
        config = _config(num_bands=1)
        settings = SimpleNamespace()  # no simulation_mode attribute
        db = _session([config, _user(), settings])
        self.fetch_midpoint.return_value = 0.5

        with self._run(db):
            result = await market_maker_service._sync_market_maker(1)

        self.credentials.assert_called_once_with("0xwallet")
        self.fetch_order_book.assert_not_awaited()
        self.fetch_midpoint.assert_awaited_once_with("token-yes")
        self.paper_sync.assert_not_awaited()
        self.assertEqual(result["midpoint"], 0.5)


class PaperSyncImplementationTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_one_simulated_trade_per_expected_order(self):
        db = MagicMock()
        config = _config(num_bands=1)
        expected = [
            {"side": "BUY", "price": 0.48, "size": 10.0, "token_id": "token-yes", "band": 0},
            {"side": "SELL", "price": 0.52, "size": 10.0, "token_id": "token-yes", "band": 0},
        ]
        book = {"best_bid": 0.48, "best_ask": 0.52, "depth": 25.0}
        fill = {"fill_price": 0.485, "slippage_bps": 2.0, "band_distance": 0.04}
        market = {"best_bid": 0.48, "best_ask": 0.52, "book_depth": 25.0}

        with (
            patch("app.models.user_trade.UserTrade") as user_trade_cls,
            patch(
                "app.services.simulation.simulate_market_maker_fill",
                return_value=fill,
            ) as simulate,
        ):
            result = await market_maker_service._sync_market_maker_paper(
                db, config, 0.5, expected, book
            )

        # Each expected order is filled against the book and
        # recorded as a simulated UserTrade row.
        self.assertEqual(
            simulate.call_args_list,
            [
                ((market, "BUY", 10.0), {"order_price": 0.48, "midpoint": 0.5}),
                ((market, "SELL", 10.0), {"order_price": 0.52, "midpoint": 0.5}),
            ],
        )
        self.assertEqual(user_trade_cls.call_count, 2)
        user_trade_cls.assert_any_call(
            user_id=7,
            market_id="cond-1",
            token_id="token-yes",
            action="buy",
            amount=10.0,
            price=0.485,
            status="simulated",
        )
        user_trade_cls.assert_any_call(
            user_id=7,
            market_id="cond-1",
            token_id="token-yes",
            action="sell",
            amount=10.0,
            price=0.485,
            status="simulated",
        )
        self.assertEqual(db.add.call_count, 2)

        # Counters and status are persisted exactly like a live cycle.
        self.assertEqual(config.total_orders_placed, 2)
        self.assertEqual(config.current_open_orders, 2)
        self.assertEqual(config.total_volume_usdc, 20.0)
        self.assertEqual(config.status, "running")
        self.assertIsNone(config.last_error)
        self.assertIsNotNone(config.last_sync_at)
        db.commit.assert_called_once_with()

        self.assertEqual(
            result,
            {
                "midpoint": 0.5,
                "expected_orders": 2,
                "cancelled": 0,
                "placed": 2,
                "simulated": True,
            },
        )

    async def test_no_expected_orders_records_nothing(self):
        db = MagicMock()
        config = _config()
        book = {"best_bid": 0.48, "best_ask": 0.52, "depth": 25.0}

        with (
            patch("app.models.user_trade.UserTrade") as user_trade_cls,
            patch("app.services.simulation.simulate_market_maker_fill") as simulate,
        ):
            result = await market_maker_service._sync_market_maker_paper(db, config, 0.5, [], book)

        simulate.assert_not_called()
        user_trade_cls.assert_not_called()
        db.add.assert_not_called()
        self.assertEqual(
            result,
            {
                "midpoint": 0.5,
                "expected_orders": 0,
                "cancelled": 0,
                "placed": 0,
                "simulated": True,
            },
        )
        self.assertEqual(config.status, "running")
        db.commit.assert_called_once_with()


# ─────────────── maker loop ───────────────


class MakerLoopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_exits_when_the_advisory_lock_is_held(self):
        with (
            patch.object(
                market_maker_service, "acquire_scheduler_lock", return_value=False
            ) as acquire,
            patch.object(market_maker_service, "SessionLocal") as session_local,
            patch.object(market_maker_service, "logger") as logger,
        ):
            await market_maker_service._maker_loop(1)

        acquire.assert_called_once_with("market_maker:1")
        session_local.assert_not_called()
        logger.info.assert_not_called()

    async def test_loop_heartbeats_and_releases_the_lock_on_shutdown(self):
        enabled = _config(enabled=True, sync_interval_seconds=1)
        disabled = _config(enabled=False)
        db = MagicMock()
        query = MagicMock()
        db.query.return_value = query
        results = [enabled, disabled, None]

        def _first():
            return results.pop(0) if results else None

        query.filter.return_value.first.side_effect = _first
        sync = AsyncMock(return_value={"placed": 0})
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "acquire_scheduler_lock", return_value=True),
            patch.object(market_maker_service, "_sync_market_maker", new=sync),
            patch.object(market_maker_service, "scheduler_heartbeat") as heartbeat,
            patch.object(market_maker_service, "release_scheduler_lock") as release,
            patch.object(market_maker_service.asyncio, "sleep", new=_fake_sleep),
        ):
            await market_maker_service._maker_loop(1)

        # One sync cycle at the configured interval, then the
        # disabled config breaks the loop.
        self.assertEqual(sleeps, [1])
        sync.assert_awaited_once_with(1)
        heartbeat.assert_called_once_with("market_maker:1")
        release.assert_called_once_with("market_maker:1")
        # Two loop iterations plus the shutdown cleanup.
        self.assertEqual(db.close.call_count, 3)

    async def test_loop_cancellation_runs_the_graceful_shutdown(self):
        # A CancelledError inside the sync is caught by the
        # loop's own handler: the loop breaks, the advisory
        # lock is released and the shutdown block cancels any
        # open orders before marking the config idle.
        config = _config(enabled=True, sync_interval_seconds=1)
        settings = SimpleNamespace(simulation_mode=False)
        db = _session([config, config, _user(), settings])
        cancel_all = AsyncMock()
        sync = AsyncMock(side_effect=asyncio.CancelledError)

        with (
            patch.object(market_maker_service, "SessionLocal", return_value=db),
            patch.object(market_maker_service, "acquire_scheduler_lock", return_value=True),
            patch.object(market_maker_service, "_sync_market_maker", new=sync),
            patch.object(market_maker_service, "release_scheduler_lock") as release,
            patch.object(market_maker_service, "_cancel_all_orders", new=cancel_all),
            patch.object(
                market_maker_service,
                "load_wallet_credentials",
                return_value={"private_key": "0xpk"},
            ),
        ):
            await market_maker_service._maker_loop(1)

        sync.assert_awaited_once_with(1)
        release.assert_called_once_with("market_maker:1")
        cancel_all.assert_awaited_once_with("0xpk", "token-yes")
        self.assertEqual(config.status, "idle")
        self.assertEqual(config.current_open_orders, 0)
        db.commit.assert_called_once_with()


# ─────────────── shutdown broadcast ───────────────


class StopAllMarketMakersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._restore = dict(market_maker_service._running_makers)
        market_maker_service._running_makers.clear()
        self.addCleanup(market_maker_service._running_makers.update, self._restore)

    async def test_broadcasts_the_shutdown(self):
        logger = MagicMock()

        with patch.object(market_maker_service, "logger", logger):
            await market_maker_service.stop_all_market_makers()

        self.assertEqual(market_maker_service._running_makers, {})
        logger.info.assert_called_once_with("All market makers stopped")

    async def test_stops_every_registered_maker(self):
        pending = asyncio.create_task(asyncio.Event().wait())
        market_maker_service._running_makers[1] = pending
        self.addCleanup(pending.cancel)
        logger = MagicMock()

        with patch.object(market_maker_service, "logger", logger):
            await market_maker_service.stop_all_market_makers()

        self.assertTrue(pending.cancelled())
        self.assertEqual(market_maker_service._running_makers, {})
        logger.info.assert_called_once_with("All market makers stopped")


# ─────────────── band ceiling (boundary) ───────────────


class MaxBandsBoundaryTests(unittest.TestCase):
    def test_max_bands_constant_is_twenty(self):
        self.assertEqual(MAX_BANDS, 20)

    def test_exactly_max_bands_is_valid(self):
        self.assertIsNone(market_maker_service._validate_max_bands(MAX_BANDS))

    def test_one_over_max_bands_raises(self):
        with self.assertRaises(ValueError):
            market_maker_service._validate_max_bands(MAX_BANDS + 1)


if __name__ == "__main__":
    unittest.main()
