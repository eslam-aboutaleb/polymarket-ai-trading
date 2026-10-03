"""Coverage tests for the latency-arbitrage engine (``app.services.latency_arb_service``).

Complements the existing paper/discovery/model tests by covering
the engine's supporting machinery with every I/O boundary mocked:

* latency sampling / percentile / distribution logging,
* ``Opportunity`` serialization (``to_dict`` / ``window_end_epoch``),
* opportunity evaluation with an explicit edge threshold,
* the opportunities board (store / read / cache failure paths),
* engine status, symbol/window parsing and default config staging,
* config getters (``get_config`` / ``get_or_create_config``),
* risk gates: per-strategy daily loss and the circuit breaker,
* the public CLOB book fetch and the depth/spread gates,
* FOK order placement (share rounding, non-dict responses),
* paper execution (simulated fill recording, engine unavailable),
* live execution (credential store, proxy resolution, FOK failure,
  exchange rejection, pending trade recording),
* ``_execute_opportunity`` — every gate and both execution paths,
* opportunity alert dispatch (async, import failure, no loop),
* opportunity discovery (every skip branch, late-entry halt),
* ``_enabled_configs`` / ``_execute_for_users`` (filters, counts,
  exception rollback),
* calculation-details parsing and trade settlement (win/loss/skip
  branches),
* the engine cycle, loop and start/stop lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.latency_arb_service as latency_arb_service
from app.models.latency_arb_config import LatencyArbConfig
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.crypto_markets_service import CryptoMarket
from app.services.latency_arb_service import (
    Opportunity,
    _book_gates,
    _calculation_details,
    _circuit_breaker_state,
    _default_config,
    _dispatch_opportunity_alert,
    _enabled_configs,
    _engine_cycle,
    _engine_loop,
    _engine_symbols,
    _engine_windows,
    _execute_for_users,
    _execute_live,
    _execute_opportunity,
    _execute_paper,
    _fetch_book,
    _fetch_opportunities,
    _log_latency_distribution,
    _parse_calculation_details,
    _percentile,
    _place_fok_order,
    _record_latency_sample,
    _settle_expired_trades,
    _store_opportunities,
    _strategy_daily_loss,
    evaluate_opportunity,
    get_config,
    get_engine_status,
    get_latest_opportunities,
    get_or_create_config,
    latency_stats,
    start_latency_arb_engine,
    stop_latency_arb_engine,
)


def _engine_settings(**overrides):
    base = {
        "latency_arb_live": False,
        "latency_arb_edge_threshold": 0.03,
        "latency_arb_max_notional": 50.0,
        "latency_arb_max_latency_ms": 1500,
        "latency_arb_cycle_seconds": 5,
        "latency_arb_daily_loss_limit": 20.0,
        "latency_arb_circuit_breaker_losses": 3,
        "latency_arb_circuit_breaker_resume_seconds": 600,
        "latency_arb_late_entry": False,
        "latency_arb_late_entry_seconds": 10,
        "latency_arb_min_depth": 25.0,
        "latency_arb_max_spread": 0.02,
        "latency_arb_symbols": "BTC,ETH,SOL",
        "latency_arb_windows": "5,15,60",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _market(up_price=0.5, down_price=0.5, window_start_epoch=None):
    now = datetime.now(UTC)
    if window_start_epoch is None:
        window_start_epoch = int(now.timestamp()) - (int(now.timestamp()) % 300)
    return CryptoMarket(
        symbol="BTC",
        window_minutes=5,
        window_start_epoch=window_start_epoch,
        condition_id="cond-btc-5m",
        token_ids={"up": "token-up-1", "down": "token-down-1"},
        prices={"up": up_price, "down": down_price},
        question="Bitcoin up or down in 5 minutes?",
    )


def _opportunity(**overrides):
    market = _market()
    base = {
        "symbol": "BTC",
        "window_minutes": 5,
        "window_start_epoch": market.window_start_epoch,
        "side": "up",
        "p_model": 0.6,
        "p_market": 0.5,
        "edge": 0.1,
        "distance": 0.01,
        "t_remaining": 0.5,
        "sigma": 0.01,
        "current_price": 67000.0,
        "window_open": 67000.0,
        "market": market,
        "detected_at": datetime.now(UTC),
        "feed_lag_ms": 50.0,
    }
    base.update(overrides)
    return Opportunity(**base)


def _user_settings(simulation_mode=True):
    return SimpleNamespace(
        user_id=7,
        simulation_mode=simulation_mode,
        paper_balance=1000.0,
        trading_halted=False,
        halt_reason=None,
        cooldown_until=None,
        monthly_loss_limit=None,
        max_drawdown_pct=25.0,
        peak_capital=None,
        total_loss_halt_pct=40.0,
        initial_capital=None,
        max_position_size=100.0,
        daily_loss_limit=500.0,
    )


def _user():
    return SimpleNamespace(id=7, wallet_address="0xwallet")


def _config(**overrides):
    base = {
        "user_id": 7,
        "enabled": True,
        "edge_threshold": 0.03,
        "max_notional": 50.0,
        "symbols": ["BTC"],
        "windows": [5],
        "late_entry": False,
        "daily_loss_limit": 20.0,
        "alert_on_opportunity": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _session():
    db = MagicMock(name="db")
    query = MagicMock(name="query")
    db.query.return_value = query
    query.filter.return_value.scalar.return_value = 0.0
    query.filter.return_value.first.return_value = None
    query.filter.return_value.all.return_value = []
    query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    return db


def _calm_closes(n: int = 60) -> list[float]:
    return [67000.0 + (20.0 if i % 2 else -20.0) for i in range(n)]


def _httpx_client(response):
    client = MagicMock(name="async_client")
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _response(status_code=200, json_data=None):
    response = MagicMock(name="response")
    response.status_code = status_code
    response.json = MagicMock(return_value=json_data)
    return response


# ── Latency distribution ──────────────────────────────────


class LatencyDistributionTests(unittest.TestCase):
    def setUp(self):
        latency_arb_service._latency_samples.clear()

    def tearDown(self):
        latency_arb_service._latency_samples.clear()

    def test_record_and_stats(self):
        _record_latency_sample(10.0, 20.0)
        _record_latency_sample(30.0, 40.0)
        stats = latency_stats()
        self.assertEqual(stats["samples"], 2)
        self.assertEqual(stats["feed_lag_p50_ms"], 30.0)
        self.assertEqual(stats["feed_lag_p95_ms"], 30.0)
        self.assertEqual(stats["total_p50_ms"], 40.0)
        self.assertEqual(stats["total_p95_ms"], 40.0)

    def test_percentile_empty(self):
        self.assertEqual(_percentile([], 50), 0.0)

    def test_percentile_values(self):
        self.assertEqual(_percentile([3.0, 1.0, 2.0], 50), 2.0)
        self.assertEqual(_percentile([1.0, 2.0, 3.0, 4.0], 95), 4.0)
        self.assertEqual(_percentile([5.0], 50), 5.0)

    def test_log_latency_distribution(self):
        _record_latency_sample(10.0, 20.0)
        with self.assertLogs(latency_arb_service.logger, level="INFO"):
            _log_latency_distribution()

    def test_log_latency_distribution_without_samples(self):
        _log_latency_distribution()  # no-op, no error


class EdgeModelEdgeCasesTests(unittest.TestCase):
    """Remaining pure-function edge branches."""

    def test_compute_t_remaining_zero_window(self):
        now = datetime.now(UTC)
        # A zero-minute window has no duration → 0.0 remaining.
        self.assertEqual(
            latency_arb_service.compute_t_remaining(now.timestamp(), 0, now),
            0.0,
        )

    def test_realized_volatility_all_non_positive_closes(self):
        # No valid log returns can be computed from
        # non-positive closes → fallback σ.
        from app.services.latency_arb_service import (
            DEFAULT_SIGMA,
            realized_volatility,
        )

        self.assertEqual(realized_volatility([0.0, 0.0, 0.0], 5), DEFAULT_SIGMA)
        self.assertEqual(realized_volatility([-1.0, -2.0], 5), DEFAULT_SIGMA)

    def test_implied_volatility_non_finite_z_score(self):
        # A non-finite z-score carries no volatility
        # information → implied σ is 0.
        with patch.object(latency_arb_service, "normal_ppf", return_value=float("inf")):
            self.assertEqual(latency_arb_service.implied_volatility(0.5, 0.5), 0.0)


# ── Opportunity serialization ─────────────────────────────


class OpportunitySerializationTests(unittest.TestCase):
    def test_window_end_epoch(self):
        opportunity = _opportunity(window_start_epoch=1_700_000_000, window_minutes=5)
        self.assertEqual(opportunity.window_end_epoch, 1_700_000_300)

    def test_to_dict(self):
        opportunity = _opportunity()
        data = opportunity.to_dict()
        self.assertEqual(data["symbol"], "BTC")
        self.assertEqual(data["window_minutes"], 5)
        self.assertEqual(data["side"], "up")
        self.assertEqual(data["p_model"], 0.6)
        self.assertEqual(data["p_market"], 0.5)
        self.assertEqual(data["edge"], 0.1)
        self.assertEqual(data["current_price"], 67000.0)
        self.assertEqual(data["window_open"], 67000.0)
        self.assertEqual(data["condition_id"], "cond-btc-5m")
        self.assertEqual(data["question"], "Bitcoin up or down in 5 minutes?")
        self.assertEqual(data["token_ids"], {"up": "token-up-1", "down": "token-down-1"})
        self.assertEqual(data["prices"], {"up": 0.5, "down": 0.5})
        self.assertEqual(data["feed_lag_ms"], 50.0)
        self.assertIsNotNone(data["detected_at"])
        self.assertEqual(
            data["window_end_epoch"],
            opportunity.window_start_epoch + 300,
        )

    def test_to_dict_without_feed_lag(self):
        data = _opportunity(feed_lag_ms=None).to_dict()
        self.assertIsNone(data["feed_lag_ms"])


# ── Opportunity evaluation ────────────────────────────────


class EvaluateOpportunityTests(unittest.TestCase):
    def test_threshold_override_produces_candidate(self):
        now = datetime.now(UTC)
        market = _market(up_price=0.5)
        opportunity = evaluate_opportunity(
            "btc",
            5,
            market,
            67000.0 * 1.05,
            67000.0,
            now,
            _calm_closes(),
            edge_threshold=0.0,
        )
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity.symbol, "BTC")
        self.assertEqual(opportunity.window_minutes, 5)
        self.assertEqual(opportunity.market, market)
        self.assertEqual(opportunity.detected_at, now)

    def test_high_threshold_returns_none(self):
        now = datetime.now(UTC)
        market = _market(up_price=0.5)
        # No move since the window opened → model ≈ market.
        opportunity = evaluate_opportunity(
            "btc",
            5,
            market,
            67000.0,
            67000.0,
            now,
            _calm_closes(),
            edge_threshold=0.5,
        )
        self.assertIsNone(opportunity)

    def test_expired_window_returns_none(self):
        now = datetime.now(UTC)
        market = _market(
            up_price=0.5,
            window_start_epoch=int(now.timestamp()) - 3600,
        )
        opportunity = evaluate_opportunity(
            "btc",
            5,
            market,
            67000.0 * 1.05,
            67000.0,
            now,
            _calm_closes(),
            edge_threshold=0.0,
        )
        self.assertIsNone(opportunity)

    def test_default_threshold_from_settings(self):
        now = datetime.now(UTC)
        market = _market(up_price=0.5)
        with patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()):
            opportunity = evaluate_opportunity(
                "btc", 5, market, 67000.0 * 1.05, 67000.0, now, _calm_closes()
            )
        self.assertIsNotNone(opportunity)


# ── Opportunities board ───────────────────────────────────


class OpportunitiesBoardTests(unittest.TestCase):
    def tearDown(self):
        latency_arb_service._opportunities = []
        latency_arb_service._opportunities_at = 0.0

    def test_store_opportunities(self):
        cache = MagicMock(name="cache")
        opportunity = _opportunity()
        with patch.object(latency_arb_service, "get_cache", return_value=cache):
            _store_opportunities([opportunity])
        self.assertEqual(len(latency_arb_service._opportunities), 1)
        self.assertNotEqual(latency_arb_service._opportunities_at, 0.0)
        cache.set.assert_called_once()
        args, kwargs = cache.set.call_args
        self.assertEqual(args[0], "opportunities")
        self.assertEqual(len(args[1]), 1)
        self.assertEqual(kwargs["ttl_seconds"], 60)

    def test_store_opportunities_cache_failure_is_swallowed(self):
        cache = MagicMock(name="cache")
        cache.set.side_effect = RuntimeError("cache down")
        with (
            patch.object(latency_arb_service, "get_cache", return_value=cache),
            self.assertLogs(level="DEBUG"),
        ):
            _store_opportunities([_opportunity()])
        self.assertEqual(len(latency_arb_service._opportunities), 1)

    def test_get_latest_opportunities_cache_hit(self):
        cache = MagicMock(name="cache")
        cache.get.return_value = [{"symbol": "BTC"}]
        with patch.object(latency_arb_service, "get_cache", return_value=cache):
            self.assertEqual(get_latest_opportunities(), [{"symbol": "BTC"}])

    def test_get_latest_opportunities_cache_miss(self):
        cache = MagicMock(name="cache")
        cache.get.return_value = None
        latency_arb_service._opportunities = [{"symbol": "ETH"}]
        with patch.object(latency_arb_service, "get_cache", return_value=cache):
            self.assertEqual(get_latest_opportunities(), [{"symbol": "ETH"}])

    def test_get_latest_opportunities_cache_error(self):
        cache = MagicMock(name="cache")
        cache.get.side_effect = RuntimeError("down")
        latency_arb_service._opportunities = [{"symbol": "SOL"}]
        with (
            patch.object(latency_arb_service, "get_cache", return_value=cache),
            self.assertLogs(level="DEBUG"),
        ):
            self.assertEqual(get_latest_opportunities(), [{"symbol": "SOL"}])

    def test_get_latest_opportunities_non_list_cache_value(self):
        cache = MagicMock(name="cache")
        cache.get.return_value = "not-a-list"
        latency_arb_service._opportunities = []
        with patch.object(latency_arb_service, "get_cache", return_value=cache):
            self.assertEqual(get_latest_opportunities(), [])


class EngineStatusTests(unittest.TestCase):
    def tearDown(self):
        latency_arb_service._running = False
        latency_arb_service._opportunities_at = 0.0

    def test_status_without_last_cycle(self):
        latency_arb_service._running = True
        latency_arb_service._opportunities_at = 0.0
        with patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()):
            status = get_engine_status()
        self.assertTrue(status["running"])
        self.assertFalse(status["live_mode"])
        self.assertIsNone(status["last_cycle_at"])
        self.assertEqual(status["cycle_seconds"], 5)
        self.assertEqual(status["symbols"], ["BTC", "ETH", "SOL"])
        self.assertEqual(status["windows"], [5, 15, 60])

    def test_status_with_last_cycle(self):
        latency_arb_service._running = False
        latency_arb_service._opportunities_at = datetime.now(UTC).timestamp()
        with patch.object(
            latency_arb_service, "get_settings", lambda: _engine_settings(latency_arb_live=True)
        ):
            status = get_engine_status()
        self.assertFalse(status["running"])
        self.assertTrue(status["live_mode"])
        self.assertIsNotNone(status["last_cycle_at"])


class EngineParsingTests(unittest.TestCase):
    def test_engine_symbols_strips_and_uppercases(self):
        ns = _engine_settings(latency_arb_symbols=" btc , eth ,, sol ")
        with patch.object(latency_arb_service, "get_settings", lambda: ns):
            self.assertEqual(_engine_symbols(), ["BTC", "ETH", "SOL"])

    def test_engine_windows_filters_non_digits(self):
        ns = _engine_settings(latency_arb_windows="5,abc,15,,60")
        with patch.object(latency_arb_service, "get_settings", lambda: ns):
            self.assertEqual(_engine_windows(), [5, 15, 60])

    def test_default_config(self):
        with patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()):
            config = _default_config(7)
        self.assertIsInstance(config, LatencyArbConfig)
        self.assertEqual(config.user_id, 7)
        self.assertFalse(config.enabled)
        self.assertEqual(config.edge_threshold, 0.03)
        self.assertEqual(config.max_notional, 50.0)
        self.assertEqual(config.symbols, ["BTC", "ETH", "SOL"])
        self.assertEqual(config.windows, [5, 15, 60])
        self.assertFalse(config.late_entry)
        self.assertEqual(config.daily_loss_limit, 20.0)
        self.assertTrue(config.alert_on_opportunity)


class ConfigGetterTests(unittest.TestCase):
    def test_get_config_returns_stored_config(self):
        db = MagicMock(name="db")
        config = MagicMock(name="config")
        db.query.return_value.filter.return_value.first.return_value = config
        self.assertIs(get_config(7, db), config)

    def test_get_config_returns_default_when_unset(self):
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.first.return_value = None
        with patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()):
            config = get_config(7, db)
        self.assertIsInstance(config, LatencyArbConfig)
        self.assertFalse(config.enabled)

    def test_get_or_create_config_returns_stored(self):
        db = MagicMock(name="db")
        config = MagicMock(name="config")
        db.query.return_value.filter.return_value.first.return_value = config
        self.assertIs(get_or_create_config(7, db), config)
        db.add.assert_not_called()

    def test_get_or_create_config_stages_default(self):
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.first.return_value = None
        with patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()):
            config = get_or_create_config(7, db)
        self.assertIsInstance(config, LatencyArbConfig)
        db.add.assert_called_once_with(config)


# ── Risk gates ────────────────────────────────────────────


class StrategyDailyLossTests(unittest.TestCase):
    def test_sums_negative_pnl(self):
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.scalar.return_value = -25.0
        self.assertEqual(_strategy_daily_loss(db, 7), 25.0)

    def test_none_result_is_zero(self):
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.scalar.return_value = None
        self.assertEqual(_strategy_daily_loss(db, 7), 0.0)


class CircuitBreakerTests(unittest.TestCase):
    def _state(self, rows, now):
        db = MagicMock(name="db")
        (
            db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value
        ) = rows
        return _circuit_breaker_state(db, 7, now)

    def test_closed_after_a_win(self):
        now = datetime.now(UTC)
        rows = [SimpleNamespace(pnl=5.0, created_at=now, updated_at=now)]
        state = self._state(rows, now)
        self.assertFalse(state["open"])
        self.assertEqual(state["consecutive_losses"], 0)
        self.assertEqual(state["resume_in_seconds"], 0)

    def test_open_after_consecutive_losses(self):
        now = datetime.now(UTC)
        rows = [SimpleNamespace(pnl=-1.0, created_at=now, updated_at=now) for _ in range(3)]
        state = self._state(rows, now)
        self.assertTrue(state["open"])
        self.assertEqual(state["consecutive_losses"], 3)
        self.assertGreater(state["resume_in_seconds"], 0)

    def test_resumes_after_timeout(self):
        now = datetime.now(UTC)
        old = now - timedelta(seconds=3600)
        rows = [SimpleNamespace(pnl=-1.0, created_at=old, updated_at=old) for _ in range(3)]
        state = self._state(rows, now)
        self.assertFalse(state["open"])
        self.assertEqual(state["consecutive_losses"], 3)

    def test_below_loss_limit_stays_closed(self):
        now = datetime.now(UTC)
        rows = [SimpleNamespace(pnl=-1.0, created_at=now, updated_at=now) for _ in range(2)]
        state = self._state(rows, now)
        self.assertFalse(state["open"])
        self.assertEqual(state["consecutive_losses"], 2)

    def test_loss_time_falls_back_to_updated_at(self):
        now = datetime.now(UTC)
        rows = [SimpleNamespace(pnl=-1.0, created_at=None, updated_at=now) for _ in range(3)]
        state = self._state(rows, now)
        self.assertTrue(state["open"])

    def test_win_breaks_the_streak(self):
        now = datetime.now(UTC)
        rows = [
            SimpleNamespace(pnl=1.0, created_at=now, updated_at=now),
            SimpleNamespace(pnl=-1.0, created_at=now, updated_at=now),
        ]
        state = self._state(rows, now)
        self.assertFalse(state["open"])
        self.assertEqual(state["consecutive_losses"], 0)


# ── CLOB book fetch and gates ─────────────────────────────


class FetchBookTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_book_returns_dict(self):
        response = _response(200, {"bids": [[0.5, 1.0]], "asks": [[0.51, 1.0]]})
        with (
            patch("httpx.AsyncClient", return_value=_httpx_client(response)),
            patch("app.services.polymarket_service.POLYMARKET_CLOB_API", "https://clob.example"),
        ):
            book = await _fetch_book("token-1")
        self.assertEqual(book, {"bids": [[0.5, 1.0]], "asks": [[0.51, 1.0]]})

    async def test_fetch_book_non_dict_json_returns_none(self):
        response = _response(200, [1, 2, 3])
        with (
            patch("httpx.AsyncClient", return_value=_httpx_client(response)),
            patch("app.services.polymarket_service.POLYMARKET_CLOB_API", "https://clob.example"),
        ):
            self.assertIsNone(await _fetch_book("token-1"))

    async def test_fetch_book_non_200_returns_none(self):
        response = _response(404, {})
        with (
            patch("httpx.AsyncClient", return_value=_httpx_client(response)),
            patch("app.services.polymarket_service.POLYMARKET_CLOB_API", "https://clob.example"),
        ):
            self.assertIsNone(await _fetch_book("token-1"))

    async def test_fetch_book_error_returns_none(self):
        client = MagicMock(name="async_client")
        client.get = AsyncMock(side_effect=RuntimeError("timeout"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch("app.services.polymarket_service.POLYMARKET_CLOB_API", "https://clob.example"),
            self.assertLogs(level="DEBUG"),
        ):
            self.assertIsNone(await _fetch_book("token-1"))


class BookGatesTests(unittest.TestCase):
    def test_empty_book_fails_both_gates(self):
        self.assertEqual(_book_gates({}, 50.0), (False, False))

    def test_bids_only_fails_both_gates(self):
        self.assertEqual(_book_gates({"bids": [[0.5, 100.0]]}, 50.0), (False, False))

    def test_asks_only_fails_both_gates(self):
        self.assertEqual(_book_gates({"asks": [[0.5, 100.0]]}, 50.0), (False, False))

    def test_insufficient_depth(self):
        book = {"bids": [[0.499, 10.0]], "asks": [[0.501, 1000.0]]}
        depth_ok, spread_ok = _book_gates(book, 50.0)
        self.assertFalse(depth_ok)
        self.assertTrue(spread_ok)

    def test_spread_too_wide(self):
        book = {"bids": [[0.40, 1000.0]], "asks": [[0.60, 1000.0]]}
        depth_ok, spread_ok = _book_gates(book, 50.0)
        self.assertTrue(depth_ok)
        self.assertFalse(spread_ok)

    def test_gates_pass(self):
        book = {"bids": [[0.499, 1000.0]], "asks": [[0.501, 1000.0]]}
        self.assertEqual(_book_gates(book, 50.0), (True, True))

    def test_zero_midpoint_fails_spread(self):
        book = {"bids": [[0.0, 1000.0]], "asks": [[0.0, 1000.0]]}
        depth_ok, spread_ok = _book_gates(book, 50.0)
        self.assertTrue(depth_ok)
        self.assertFalse(spread_ok)

    def test_malformed_levels_ignored(self):
        book = {
            "bids": [["bad"], [0.499, 1000.0], "nope", [0.49]],
            "asks": [[0.501, 1000.0], None],
        }
        self.assertEqual(_book_gates(book, 50.0), (True, True))


class PlaceFokOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_place_fok_order_rounds_shares_up(self):
        client = MagicMock(name="clob_client")
        client.create_order.return_value = "signed-order"
        client.post_order.return_value = {"success": True}
        order_args_cls = MagicMock(name="OrderArgs")
        order_type_cls = MagicMock(name="OrderType")
        with (
            patch.object(latency_arb_service, "build_clob_client", return_value=client),
            patch("py_clob_client.clob_types.OrderArgs", order_args_cls),
            patch("py_clob_client.clob_types.OrderType", order_type_cls),
            patch("py_clob_client.order_builder.constants.BUY", "BUY"),
        ):
            response = await _place_fok_order("0xkey", None, "0xproxy", "token-1", 0.5, 50.0)
        self.assertEqual(response, {"success": True})
        # 50 USDC / 0.5 = 100 shares exactly.
        order_args_cls.assert_called_once_with(
            token_id="token-1", price=0.5, size=100.0, side="BUY"
        )
        client.create_order.assert_called_once_with(order_args_cls.return_value)
        client.post_order.assert_called_once_with("signed-order", order_type_cls.FOK)

    async def test_place_fok_order_returns_empty_dict_for_non_dict(self):
        client = MagicMock(name="clob_client")
        client.create_order.return_value = "signed-order"
        client.post_order.return_value = "not-a-dict"
        with (
            patch.object(latency_arb_service, "build_clob_client", return_value=client),
            patch("py_clob_client.clob_types.OrderArgs"),
            patch("py_clob_client.clob_types.OrderType"),
            patch("py_clob_client.order_builder.constants.BUY", "BUY"),
        ):
            response = await _place_fok_order("0xkey", None, None, "token-1", 0.5, 50.0)
        self.assertEqual(response, {})


class CalculationDetailsTests(unittest.TestCase):
    def test_calculation_details_merges_extra(self):
        opportunity = _opportunity()
        details = json.loads(_calculation_details(opportunity, {"mode": "paper"}))
        self.assertEqual(details["strategy"], "latency_arb")
        self.assertEqual(details["symbol"], "BTC")
        self.assertEqual(details["window_minutes"], 5)
        self.assertEqual(details["window_open"], 67000.0)
        self.assertEqual(details["current_price"], 67000.0)
        self.assertEqual(details["side"], "up")
        self.assertEqual(details["p_model"], 0.6)
        self.assertEqual(details["p_market"], 0.5)
        self.assertEqual(details["edge"], 0.1)
        self.assertEqual(details["feed_lag_ms"], 50.0)
        self.assertEqual(details["mode"], "paper")

    def test_calculation_details_without_feed_lag(self):
        opportunity = _opportunity(feed_lag_ms=None)
        details = json.loads(_calculation_details(opportunity, {}))
        self.assertIsNone(details["feed_lag_ms"])


# ── Execution: paper ──────────────────────────────────────


class ExecutePaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_paper_records_simulated_fill(self):
        db = _session()
        settings = _user_settings()
        opportunity = _opportunity()
        fill = {"fill_price": 0.51, "slippage_bps": 100.0}
        with (
            patch.object(latency_arb_service, "_SIMULATION_AVAILABLE", True),
            patch.object(latency_arb_service, "simulate_fill", MagicMock(return_value=fill)),
        ):
            result = await _execute_paper(db, settings, opportunity, "token-up-1", 0.5, 50.0, 100.0)
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["fill_price"], 0.51)
        self.assertEqual(result["slippage_bps"], 100.0)
        self.assertEqual(result["size"], 50.0)
        trade = db.add.call_args.args[0]
        self.assertEqual(trade.user_id, 7)
        self.assertEqual(trade.market_id, "cond-btc-5m")
        self.assertEqual(trade.token_id, "token-up-1")
        self.assertEqual(trade.action, "buy")
        self.assertEqual(trade.amount, 50.0)
        self.assertEqual(trade.price, 0.51)
        self.assertEqual(trade.status, "simulated")
        self.assertEqual(trade.expected_price, 0.5)
        self.assertEqual(trade.expected_size, 50.0)
        self.assertEqual(trade.filled_price, 0.51)
        self.assertEqual(trade.filled_size, 50.0)
        self.assertEqual(trade.slippage_bps, 100.0)
        self.assertEqual(trade.latency_ms, 100.0)
        self.assertEqual(trade.strategy_source, "latency_arb")
        self.assertIsNotNone(trade.executed_at)
        self.assertIn("p_model", trade.calculation_details)
        db.commit.assert_called_once()
        db.refresh.assert_called_once()

    async def test_paper_rejected_when_simulation_unavailable(self):
        db = _session()
        settings = _user_settings()
        opportunity = _opportunity()
        with (
            patch.object(latency_arb_service, "_SIMULATION_AVAILABLE", False),
            patch.object(latency_arb_service, "simulate_fill", None),
        ):
            result = await _execute_paper(db, settings, opportunity, "token-up-1", 0.5, 50.0, 100.0)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "simulation engine unavailable")
        db.add.assert_not_called()


# ── Execution: live ───────────────────────────────────────


class ExecuteLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_rejected_without_pre_trade_gate(self):
        db = _session()
        with (
            patch.object(latency_arb_service, "_PRE_TRADE_GATE_AVAILABLE", False),
            patch.object(latency_arb_service, "build_clob_client", None),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "pre-trade gate unavailable")

    async def test_live_rejected_on_credential_store_error(self):
        from app.security.credential_store import CredentialStoreError

        db = _session()
        with patch(
            "app.security.credential_store.load_wallet_credentials",
            side_effect=CredentialStoreError("vault locked"),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("credential store unavailable", result["reason"])
        self.assertIn("vault locked", result["reason"])

    async def test_live_rejected_without_stored_credentials(self):
        db = _session()
        with patch(
            "app.security.credential_store.load_wallet_credentials",
            return_value=None,
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "no stored wallet credentials")

    async def test_live_resolves_proxy_address(self):
        db = _session()
        place = AsyncMock(return_value={"success": True, "orderID": "0xabc"})
        signer_cls = MagicMock(name="Signer")
        signer_cls.return_value.address.return_value = "0xeoa"
        with (
            patch(
                "app.security.credential_store.load_wallet_credentials",
                return_value={"private_key": "0x" + "11" * 32, "clob_creds": None},
            ),
            patch.object(
                latency_arb_service,
                "_get_poly_proxy_wallet_address",
                return_value="0xproxy",
            ),
            patch("py_clob_client.signer.Signer", signer_cls),
            patch.object(latency_arb_service, "_place_fok_order", place),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["order_hash"], "0xabc")
        place.assert_awaited_once()
        # (private_key, clob_creds, proxy_address, token_id, price, size)
        self.assertEqual(place.await_args.args[2], "0xproxy")
        signer_cls.assert_called_once_with("0x" + "11" * 32, 137)

    async def test_live_skips_proxy_when_unavailable(self):
        db = _session()
        place = AsyncMock(return_value={"success": True, "orderID": "0xabc"})
        with (
            patch(
                "app.security.credential_store.load_wallet_credentials",
                return_value={"private_key": "0x" + "11" * 32, "clob_creds": None},
            ),
            patch.object(latency_arb_service, "_get_poly_proxy_wallet_address", None),
            patch.object(latency_arb_service, "_place_fok_order", place),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(place.await_args.args[2], None)

    async def test_live_fok_failure(self):
        db = _session()
        place = AsyncMock(side_effect=RuntimeError("order failed"))
        with (
            patch(
                "app.security.credential_store.load_wallet_credentials",
                return_value={"private_key": "0x" + "11" * 32, "clob_creds": None},
            ),
            patch.object(latency_arb_service, "_get_poly_proxy_wallet_address", None),
            patch.object(latency_arb_service, "_place_fok_order", place),
            self.assertLogs(latency_arb_service.logger, level="WARNING"),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "order failed")
        db.add.assert_not_called()

    async def test_live_exchange_rejection_with_error_msg(self):
        db = _session()
        place = AsyncMock(return_value={"success": False, "errorMsg": "rejected by exchange"})
        with (
            patch(
                "app.security.credential_store.load_wallet_credentials",
                return_value={"private_key": "0x" + "11" * 32, "clob_creds": None},
            ),
            patch.object(latency_arb_service, "_get_poly_proxy_wallet_address", None),
            patch.object(latency_arb_service, "_place_fok_order", place),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "rejected by exchange")
        db.add.assert_not_called()

    async def test_live_error_field_only_records_pending_trade(self):
        # An ``error`` field without ``success: false`` or
        # ``errorMsg`` does not trigger the rejection branch;
        # the order is recorded pending with an empty hash.
        db = _session()
        place = AsyncMock(return_value={"error": "some error"})
        with (
            patch(
                "app.security.credential_store.load_wallet_credentials",
                return_value={"private_key": "0x" + "11" * 32, "clob_creds": None},
            ),
            patch.object(latency_arb_service, "_get_poly_proxy_wallet_address", None),
            patch.object(latency_arb_service, "_place_fok_order", place),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["order_hash"], "")
        trade = db.add.call_args.args[0]
        self.assertEqual(trade.status, "pending")
        self.assertEqual(trade.order_hash, "")

    async def test_live_records_pending_trade(self):
        db = _session()
        place = AsyncMock(return_value={"success": True, "order_id": "0xdef"})
        with (
            patch(
                "app.security.credential_store.load_wallet_credentials",
                return_value={"private_key": "0x" + "11" * 32, "clob_creds": None},
            ),
            patch.object(latency_arb_service, "_get_poly_proxy_wallet_address", None),
            patch.object(latency_arb_service, "_place_fok_order", place),
        ):
            result = await _execute_live(
                db,
                _user(),
                _user_settings(simulation_mode=False),
                _opportunity(),
                "token-1",
                0.5,
                50.0,
                100.0,
            )
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["order_hash"], "0xdef")  # order_id fallback
        trade = db.add.call_args.args[0]
        self.assertEqual(trade.status, "pending")
        self.assertEqual(trade.order_hash, "0xdef")
        self.assertEqual(trade.price, 0.5)
        self.assertEqual(trade.amount, 50.0)
        self.assertEqual(trade.expected_price, 0.5)
        self.assertEqual(trade.expected_size, 50.0)
        self.assertIsNone(trade.executed_at)
        self.assertEqual(trade.strategy_source, "latency_arb")
        db.commit.assert_called_once()


# ── Opportunity execution gates ───────────────────────────


class ExecuteOpportunityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.samples_before = latency_stats()["samples"]

    def tearDown(self):
        latency_arb_service._latency_samples.clear()

    async def _execute(
        self,
        opportunity=None,
        settings_ns=None,
        config=None,
        user_settings=None,
        user=None,
        now=None,
        db=None,
        extra_patches=(),
    ):
        opportunity = opportunity or _opportunity()
        settings_ns = settings_ns or _engine_settings()
        config = config or _config()
        user_settings = user_settings or _user_settings()
        user = user or _user()
        now = now or datetime.now(UTC)
        db = db or _session()
        patches = [
            patch.object(latency_arb_service, "get_settings", lambda: settings_ns),
            patch.object(latency_arb_service, "SessionLocal", return_value=db),
            patch.object(
                latency_arb_service,
                "apply_global_safety_caps",
                MagicMock(return_value=(50.0, None)),
            ),
            patch.object(
                latency_arb_service,
                "_strategy_daily_loss",
                MagicMock(return_value=0.0),
            ),
            patch.object(
                latency_arb_service,
                "_circuit_breaker_state",
                MagicMock(
                    return_value={
                        "open": False,
                        "consecutive_losses": 0,
                        "resume_in_seconds": 0,
                    }
                ),
            ),
            patch.object(latency_arb_service, "_fetch_book", AsyncMock(return_value=None)),
            patch.object(
                latency_arb_service,
                "_execute_paper",
                AsyncMock(return_value={"status": "simulated"}),
            ),
            patch.object(
                latency_arb_service,
                "_execute_live",
                AsyncMock(return_value={"status": "submitted"}),
            ),
        ]
        patches.extend(extra_patches)
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return await _execute_opportunity(db, config, user_settings, user, opportunity, now)

    async def test_live_mode_disabled_skips(self):
        result = await self._execute(
            settings_ns=_engine_settings(latency_arb_live=False),
            user_settings=_user_settings(simulation_mode=False),
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "live mode disabled")

    async def test_latency_budget_exceeded_aborts(self):
        opportunity = _opportunity(
            detected_at=datetime.now(UTC) - timedelta(seconds=10),
            feed_lag_ms=2000.0,
        )
        result = await self._execute(opportunity=opportunity)
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["reason"], "latency budget exceeded")
        self.assertGreater(result["feed_to_order_ms"], 1500.0)
        # The aborted path still records a latency sample.
        self.assertEqual(latency_stats()["samples"], self.samples_before + 1)

    async def test_zero_size_rejected(self):
        result = await self._execute(config=_config(max_notional=0.0))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "zero trade size")

    async def test_pre_trade_gate_unavailable(self):
        result = await self._execute(
            extra_patches=[
                patch.object(latency_arb_service, "_PRE_TRADE_GATE_AVAILABLE", False),
                patch.object(latency_arb_service, "apply_global_safety_caps", None),
            ]
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "pre-trade gate unavailable")

    async def test_global_safety_caps_rejection(self):
        result = await self._execute(
            extra_patches=[
                patch.object(
                    latency_arb_service,
                    "apply_global_safety_caps",
                    MagicMock(return_value=(50.0, "trading halted")),
                ),
            ]
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "trading halted")

    async def test_global_safety_caps_adjusts_size(self):
        paper = AsyncMock(return_value={"status": "simulated"})
        result = await self._execute(
            user_settings=_user_settings(simulation_mode=True),
            extra_patches=[
                patch.object(
                    latency_arb_service,
                    "apply_global_safety_caps",
                    MagicMock(return_value=(25.0, None)),
                ),
                patch.object(latency_arb_service, "_execute_paper", paper),
            ],
        )
        self.assertEqual(result["status"], "simulated")
        # size passed to paper execution is the capped 25.0.
        self.assertEqual(paper.await_args.args[5], 25.0)

    async def test_strategy_daily_loss_limit_rejects(self):
        result = await self._execute(
            config=_config(daily_loss_limit=20.0),
            extra_patches=[
                patch.object(
                    latency_arb_service,
                    "_strategy_daily_loss",
                    MagicMock(return_value=25.0),
                ),
            ],
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "strategy daily loss limit reached")
        self.assertEqual(result["daily_loss"], 25.0)

    async def test_circuit_breaker_open_rejects(self):
        result = await self._execute(
            extra_patches=[
                patch.object(
                    latency_arb_service,
                    "_circuit_breaker_state",
                    MagicMock(
                        return_value={
                            "open": True,
                            "consecutive_losses": 3,
                            "resume_in_seconds": 300,
                        }
                    ),
                ),
            ]
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("circuit breaker open", result["reason"])
        self.assertIn("3 consecutive losses", result["reason"])
        self.assertIn("300s", result["reason"])

    async def test_market_data_incomplete_rejects_missing_token(self):
        market = _market()
        market.token_ids = {"up": None, "down": "token-down-1"}
        result = await self._execute(opportunity=_opportunity(market=market, side="up"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "market data incomplete")

    async def test_market_data_incomplete_rejects_zero_price(self):
        market = _market(up_price=0.0)
        result = await self._execute(opportunity=_opportunity(market=market, side="up"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "market data incomplete")

    async def test_live_book_depth_rejects(self):
        book = {"bids": [[0.499, 10.0]], "asks": [[0.501, 1000.0]]}
        result = await self._execute(
            settings_ns=_engine_settings(latency_arb_live=True),
            user_settings=_user_settings(simulation_mode=False),
            extra_patches=[
                patch.object(latency_arb_service, "_fetch_book", AsyncMock(return_value=book)),
            ],
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "insufficient depth")

    async def test_live_book_spread_aborts(self):
        book = {"bids": [[0.40, 1000.0]], "asks": [[0.60, 1000.0]]}
        result = await self._execute(
            settings_ns=_engine_settings(latency_arb_live=True),
            user_settings=_user_settings(simulation_mode=False),
            extra_patches=[
                patch.object(latency_arb_service, "_fetch_book", AsyncMock(return_value=book)),
            ],
        )
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["reason"], "spread too wide")

    async def test_live_book_fetch_failure_skips_gates(self):
        live = AsyncMock(return_value={"status": "submitted"})
        result = await self._execute(
            settings_ns=_engine_settings(latency_arb_live=True),
            user_settings=_user_settings(simulation_mode=False),
            extra_patches=[
                patch.object(latency_arb_service, "_fetch_book", AsyncMock(return_value=None)),
                patch.object(latency_arb_service, "_execute_live", live),
            ],
        )
        self.assertEqual(result["status"], "submitted")
        live.assert_awaited_once()

    async def test_paper_path_executes_and_records_latency(self):
        paper = AsyncMock(return_value={"status": "simulated", "trade_id": 1})
        result = await self._execute(
            user_settings=_user_settings(simulation_mode=True),
            extra_patches=[
                patch.object(latency_arb_service, "_execute_paper", paper),
            ],
        )
        self.assertEqual(result["status"], "simulated")
        paper.assert_awaited_once()
        # (db, settings, opportunity, token_id, reference_price, size, feed_to_order_ms)
        self.assertEqual(paper.await_args.args[3], "token-up-1")
        self.assertEqual(paper.await_args.args[4], 0.5)
        self.assertEqual(paper.await_args.args[5], 50.0)
        self.assertEqual(latency_stats()["samples"], self.samples_before + 1)

    async def test_live_path_executes(self):
        live = AsyncMock(return_value={"status": "submitted", "order_hash": "0xabc"})
        result = await self._execute(
            settings_ns=_engine_settings(latency_arb_live=True),
            user_settings=_user_settings(simulation_mode=False),
            extra_patches=[
                patch.object(latency_arb_service, "_execute_live", live),
            ],
        )
        self.assertEqual(result["status"], "submitted")
        live.assert_awaited_once()
        # (db, user, settings, opportunity, token_id, ...)
        self.assertEqual(live.await_args.args[4], "token-up-1")

    async def test_down_side_uses_down_leg(self):
        paper = AsyncMock(return_value={"status": "simulated"})
        result = await self._execute(
            opportunity=_opportunity(side="down"),
            user_settings=_user_settings(simulation_mode=True),
            extra_patches=[
                patch.object(latency_arb_service, "_execute_paper", paper),
            ],
        )
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(paper.await_args.args[3], "token-down-1")
        self.assertEqual(paper.await_args.args[4], 0.5)


# ── Alert dispatch ────────────────────────────────────────


class DispatchAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_alert_sends_payload(self):
        opportunity = _opportunity()
        dispatch = AsyncMock()
        with patch("app.services.alert_service.dispatch", dispatch):
            _dispatch_opportunity_alert(7, opportunity, {"status": "simulated"})
            await asyncio.sleep(0)  # let the scheduled task run
        dispatch.assert_awaited_once()
        args, kwargs = dispatch.await_args
        self.assertEqual(args[0], "latency_arb_opportunity")
        self.assertEqual(args[1], 7)
        payload = args[2]
        self.assertEqual(payload["symbol"], "BTC")
        self.assertEqual(payload["window_minutes"], 5)
        self.assertEqual(payload["side"], "up")
        self.assertEqual(payload["p_model"], 0.6)
        self.assertEqual(payload["p_market"], 0.5)
        self.assertEqual(payload["edge"], 0.1)
        self.assertEqual(payload["condition_id"], "cond-btc-5m")
        self.assertEqual(payload["status"], "simulated")
        self.assertIn("edge", payload["message"])
        self.assertTrue(kwargs["background"])

    async def test_dispatch_alert_import_failure_is_swallowed(self):
        with (
            patch.dict("sys.modules", {"app.services.alert_service": None}),
            self.assertLogs(level="DEBUG"),
        ):
            _dispatch_opportunity_alert(7, _opportunity(), {"status": "simulated"})


class DispatchAlertSyncTests(unittest.TestCase):
    def test_dispatch_alert_without_running_loop_is_swallowed(self):
        # Synchronous context: get_running_loop() raises and
        # the dispatch is skipped.
        with self.assertLogs(level="DEBUG"):
            _dispatch_opportunity_alert(7, _opportunity(), {"status": "simulated"})


# ── Opportunity discovery ─────────────────────────────────


class FetchOpportunitiesTests(unittest.IsolatedAsyncioTestCase):
    def _patches(self, market=None, **overrides):
        market = market or _market()
        settings = {
            "get_market": MagicMock(return_value=market),
            "window_start_matches": MagicMock(return_value=True),
            "get_last_price": MagicMock(return_value=67234.5),
            "get_window_open_price": MagicMock(return_value=67000.0),
            "get_recent_closes": MagicMock(return_value=_calm_closes()),
            "get_feed_lag_ms": MagicMock(return_value=50.0),
        }
        settings.update(overrides)
        return settings

    async def _fetch(self, now, settings_ns=None, evaluate=None, **market_mocks):
        settings_ns = settings_ns or _engine_settings()
        evaluate = evaluate or MagicMock(return_value=None)
        mocks = self._patches(**market_mocks)
        with (
            patch.object(latency_arb_service, "get_settings", lambda: settings_ns),
            patch.object(latency_arb_service, "_engine_symbols", MagicMock(return_value=["BTC"])),
            patch.object(latency_arb_service, "_engine_windows", MagicMock(return_value=[5])),
            patch.object(
                latency_arb_service.crypto_markets_service, "get_market", mocks["get_market"]
            ),
            patch.object(
                latency_arb_service.crypto_markets_service,
                "window_start_matches",
                mocks["window_start_matches"],
            ),
            patch.object(
                latency_arb_service.binance_ws_service, "get_last_price", mocks["get_last_price"]
            ),
            patch.object(
                latency_arb_service.binance_ws_service,
                "get_window_open_price",
                mocks["get_window_open_price"],
            ),
            patch.object(
                latency_arb_service.binance_ws_service,
                "get_recent_closes",
                mocks["get_recent_closes"],
            ),
            patch.object(
                latency_arb_service.binance_ws_service, "get_feed_lag_ms", mocks["get_feed_lag_ms"]
            ),
            patch.object(latency_arb_service, "evaluate_opportunity", evaluate),
        ):
            return await _fetch_opportunities(now)

    async def test_missing_market_skipped(self):
        now = datetime.now(UTC)
        result = await self._fetch(now, get_market=MagicMock(return_value=None))
        self.assertEqual(result, [])

    async def test_wrong_window_skipped(self):
        now = datetime.now(UTC)
        result = await self._fetch(now, window_start_matches=MagicMock(return_value=False))
        self.assertEqual(result, [])

    async def test_missing_current_price_skipped(self):
        now = datetime.now(UTC)
        result = await self._fetch(now, get_last_price=MagicMock(return_value=None))
        self.assertEqual(result, [])

    async def test_missing_window_open_skipped(self):
        now = datetime.now(UTC)
        result = await self._fetch(now, get_window_open_price=MagicMock(return_value=None))
        self.assertEqual(result, [])

    async def test_zero_window_open_skipped(self):
        now = datetime.now(UTC)
        result = await self._fetch(now, get_window_open_price=MagicMock(return_value=0.0))
        self.assertEqual(result, [])

    async def test_late_entry_halt(self):
        now = datetime.now(UTC)
        # 5 seconds left in the window — inside the 10s halt.
        market = _market(window_start_epoch=int(now.timestamp()) - 295)
        result = await self._fetch(now, market=market)
        self.assertEqual(result, [])

    async def test_late_entry_allowed_when_enabled(self):
        now = datetime.now(UTC)
        market = _market(window_start_epoch=int(now.timestamp()) - 295)
        opportunity = _opportunity()
        result = await self._fetch(
            now,
            settings_ns=_engine_settings(latency_arb_late_entry=True),
            market=market,
            evaluate=MagicMock(return_value=opportunity),
        )
        self.assertEqual(result, [opportunity])

    async def test_none_evaluation_skipped(self):
        now = datetime.now(UTC)
        result = await self._fetch(now, evaluate=MagicMock(return_value=None))
        self.assertEqual(result, [])

    async def test_opportunity_returned(self):
        now = datetime.now(UTC)
        opportunity = _opportunity()
        result = await self._fetch(now, evaluate=MagicMock(return_value=opportunity))
        self.assertEqual(result, [opportunity])


# ── Enabled configs and per-user execution ────────────────


class EnabledConfigsTests(unittest.TestCase):
    def test_enabled_configs_filters_incomplete_rows(self):
        db = MagicMock(name="db")
        config1, config2, config3 = (
            SimpleNamespace(user_id=1),
            SimpleNamespace(user_id=2),
            SimpleNamespace(user_id=3),
        )
        settings = SimpleNamespace(user_id=3)
        user = SimpleNamespace(id=3)

        config_query = MagicMock(name="config_query")
        config_query.filter.return_value.all.return_value = [config1, config2, config3]
        settings_query = MagicMock(name="settings_query")
        settings_query.filter.return_value.first.side_effect = [None, settings, settings]
        user_query = MagicMock(name="user_query")
        user_query.filter.return_value.first.side_effect = [user, None, user]

        def _query(model):
            return {
                latency_arb_service.LatencyArbConfig: config_query,
                UserSettings: settings_query,
                User: user_query,
            }[model]

        db.query.side_effect = _query
        enabled = _enabled_configs(db)
        # config1: no settings row; config2: no user row;
        # config3: complete.
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0], (config3, settings, user))


class ExecuteForUsersTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_opportunities_returns_zero(self):
        session_cls = patch.object(latency_arb_service, "SessionLocal", return_value=_session())
        with session_cls as patched:
            self.assertEqual(await _execute_for_users([], datetime.now(UTC)), 0)
        patched.assert_not_called()

    async def test_filters_and_counts_executed(self):
        db = _session()
        now = datetime.now(UTC)
        low_edge = _opportunity(edge=0.01)
        wrong_symbol = _opportunity(symbol="ETH")
        wrong_window = _opportunity(window_minutes=15)
        good = _opportunity()
        executor = AsyncMock(side_effect=[{"status": "simulated"}, {"status": "rejected"}])
        alert = MagicMock(name="dispatch_alert")
        with (
            patch.object(latency_arb_service, "SessionLocal", return_value=db),
            patch.object(
                latency_arb_service,
                "_enabled_configs",
                MagicMock(return_value=[(_config(), _user_settings(), _user())]),
            ),
            patch.object(latency_arb_service, "_execute_opportunity", executor),
            patch.object(latency_arb_service, "_dispatch_opportunity_alert", alert),
        ):
            executed = await _execute_for_users(
                [low_edge, wrong_symbol, wrong_window, good, good], now
            )
        self.assertEqual(executed, 1)
        self.assertEqual(executor.await_count, 2)
        # Only the simulated fill alerts.
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[0], 7)

    async def test_no_alert_when_disabled(self):
        db = _session()
        now = datetime.now(UTC)
        good = _opportunity()
        executor = AsyncMock(return_value={"status": "submitted"})
        alert = MagicMock(name="dispatch_alert")
        with (
            patch.object(latency_arb_service, "SessionLocal", return_value=db),
            patch.object(
                latency_arb_service,
                "_enabled_configs",
                MagicMock(
                    return_value=[
                        (
                            _config(alert_on_opportunity=False),
                            _user_settings(simulation_mode=False),
                            _user(),
                        )
                    ]
                ),
            ),
            patch.object(latency_arb_service, "_execute_opportunity", executor),
            patch.object(latency_arb_service, "_dispatch_opportunity_alert", alert),
            patch.object(
                latency_arb_service,
                "get_settings",
                lambda: _engine_settings(latency_arb_live=True),
            ),
        ):
            executed = await _execute_for_users([good], now)
        self.assertEqual(executed, 1)
        alert.assert_not_called()

    async def test_execution_error_rolls_back(self):
        db = _session()
        now = datetime.now(UTC)
        good = _opportunity()
        executor = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(latency_arb_service, "SessionLocal", return_value=db),
            patch.object(
                latency_arb_service,
                "_enabled_configs",
                MagicMock(return_value=[(_config(), _user_settings(), _user())]),
            ),
            patch.object(latency_arb_service, "_execute_opportunity", executor),
            self.assertLogs(latency_arb_service.logger, level="WARNING"),
        ):
            executed = await _execute_for_users([good], now)
        self.assertEqual(executed, 0)
        db.rollback.assert_called_once()


class ParseCalculationDetailsTests(unittest.TestCase):
    def test_none_details(self):
        self.assertEqual(_parse_calculation_details(SimpleNamespace(calculation_details=None)), {})

    def test_invalid_json(self):
        self.assertEqual(
            _parse_calculation_details(SimpleNamespace(calculation_details="not-json{")),
            {},
        )

    def test_non_dict_json(self):
        self.assertEqual(
            _parse_calculation_details(SimpleNamespace(calculation_details="[1, 2]")),
            {},
        )

    def test_valid_dict(self):
        self.assertEqual(
            _parse_calculation_details(SimpleNamespace(calculation_details='{"symbol": "BTC"}')),
            {"symbol": "BTC"},
        )


# ── Settlement ────────────────────────────────────────────


class SettleExpiredTradesTests(unittest.IsolatedAsyncioTestCase):
    def _trade(self, details, price=0.5, amount=100.0):
        return SimpleNamespace(
            calculation_details=json.dumps(details),
            price=price,
            amount=amount,
            pnl=None,
        )

    async def _settle(self, trades, open_price=67000.0, close_price=67100.0):
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.all.return_value = trades
        with (
            patch.object(latency_arb_service, "SessionLocal", return_value=db),
            patch.object(
                latency_arb_service.binance_ws_service,
                "get_window_open_price",
                MagicMock(return_value=open_price),
            ),
            patch.object(
                latency_arb_service.binance_ws_service,
                "get_window_close_price",
                MagicMock(return_value=close_price),
            ),
        ):
            return await _settle_expired_trades(datetime.now(UTC)), db

    async def test_settles_winning_up_trade(self):
        now = datetime.now(UTC)
        details = {
            "window_start_epoch": int(now.timestamp()) - 600,
            "window_minutes": 5,
            "symbol": "BTC",
            "side": "up",
        }
        trade = self._trade(details)
        settled, db = await self._settle([trade])
        self.assertEqual(settled, 1)
        # 100 * (1 - 0.5) / 0.5 = 100.0
        self.assertAlmostEqual(trade.pnl, 100.0)
        db.commit.assert_called_once()

    async def test_settles_losing_up_trade(self):
        now = datetime.now(UTC)
        details = {
            "window_start_epoch": int(now.timestamp()) - 600,
            "window_minutes": 5,
            "symbol": "BTC",
            "side": "up",
        }
        trade = self._trade(details)
        settled, db = await self._settle([trade], close_price=66900.0)
        self.assertEqual(settled, 1)
        self.assertAlmostEqual(trade.pnl, -100.0)

    async def test_settles_winning_down_trade(self):
        now = datetime.now(UTC)
        details = {
            "window_start_epoch": int(now.timestamp()) - 600,
            "window_minutes": 5,
            "symbol": "BTC",
            "side": "down",
        }
        trade = self._trade(details)
        settled, _ = await self._settle([trade], close_price=66900.0)
        self.assertEqual(settled, 1)
        self.assertAlmostEqual(trade.pnl, 100.0)

    async def test_skips_unexpired_window(self):
        now = datetime.now(UTC)
        details = {
            "window_start_epoch": int(now.timestamp()),
            "window_minutes": 5,
            "symbol": "BTC",
            "side": "up",
        }
        settled, db = await self._settle([self._trade(details)])
        self.assertEqual(settled, 0)
        db.commit.assert_not_called()

    async def test_skips_incomplete_details(self):
        now = datetime.now(UTC)
        base = {
            "window_start_epoch": int(now.timestamp()) - 600,
            "window_minutes": 5,
            "symbol": "BTC",
            "side": "up",
        }
        trades = [
            self._trade({k: v for k, v in base.items() if k != "window_start_epoch"}),
            self._trade({k: v for k, v in base.items() if k != "window_minutes"}),
            self._trade({k: v for k, v in base.items() if k != "symbol"}),
            self._trade({**base, "side": "sideways"}),
        ]
        settled, _ = await self._settle(trades)
        self.assertEqual(settled, 0)

    async def test_skips_missing_feed_data(self):
        now = datetime.now(UTC)
        details = {
            "window_start_epoch": int(now.timestamp()) - 600,
            "window_minutes": 5,
            "symbol": "BTC",
            "side": "up",
        }
        db = MagicMock(name="db")
        db.query.return_value.filter.return_value.all.return_value = [self._trade(details)]
        with (
            patch.object(latency_arb_service, "SessionLocal", return_value=db),
            patch.object(
                latency_arb_service.binance_ws_service,
                "get_window_open_price",
                MagicMock(return_value=None),
            ),
            patch.object(
                latency_arb_service.binance_ws_service,
                "get_window_close_price",
                MagicMock(return_value=None),
            ),
        ):
            settled = await _settle_expired_trades(datetime.now(UTC))
        self.assertEqual(settled, 0)

    async def test_skips_zero_price_or_amount(self):
        now = datetime.now(UTC)
        details = {
            "window_start_epoch": int(now.timestamp()) - 600,
            "window_minutes": 5,
            "symbol": "BTC",
            "side": "up",
        }
        trades = [
            self._trade(details, price=0.0),
            self._trade(details, amount=0.0),
        ]
        settled, _ = await self._settle(trades)
        self.assertEqual(settled, 0)

    async def test_settlement_error_rolls_back(self):
        db = MagicMock(name="db")
        db.query.side_effect = RuntimeError("db down")
        with (
            patch.object(latency_arb_service, "SessionLocal", return_value=db),
            self.assertLogs(latency_arb_service.logger, level="WARNING"),
        ):
            settled = await _settle_expired_trades(datetime.now(UTC))
        self.assertEqual(settled, 0)
        db.rollback.assert_called_once()

    async def test_no_settlement_skips_commit(self):
        settled, db = await self._settle([])
        self.assertEqual(settled, 0)
        db.commit.assert_not_called()


# ── Engine cycle, loop and lifecycle ──────────────────────


class EngineCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_cycle_refreshes_stale_markets(self):
        refresh = AsyncMock()
        fetch = AsyncMock(return_value=[])
        store = MagicMock()
        log_dist = MagicMock()
        execute = AsyncMock(return_value=0)
        settle = AsyncMock(return_value=0)
        with (
            patch.object(
                latency_arb_service.crypto_markets_service,
                "markets_stale",
                MagicMock(return_value=True),
            ),
            patch.object(latency_arb_service.crypto_markets_service, "refresh_markets", refresh),
            patch.object(latency_arb_service, "_fetch_opportunities", fetch),
            patch.object(latency_arb_service, "_store_opportunities", store),
            patch.object(latency_arb_service, "_log_latency_distribution", log_dist),
            patch.object(latency_arb_service, "_execute_for_users", execute),
            patch.object(latency_arb_service, "_settle_expired_trades", settle),
        ):
            await _engine_cycle()
        refresh.assert_awaited_once()
        fetch.assert_awaited_once()
        store.assert_called_once_with([])
        log_dist.assert_called_once()
        execute.assert_awaited_once()
        settle.assert_awaited_once()

    async def test_engine_cycle_skips_refresh_when_fresh(self):
        refresh = AsyncMock()
        fetch = AsyncMock(return_value=[])
        with (
            patch.object(
                latency_arb_service.crypto_markets_service,
                "markets_stale",
                MagicMock(return_value=False),
            ),
            patch.object(latency_arb_service.crypto_markets_service, "refresh_markets", refresh),
            patch.object(latency_arb_service, "_fetch_opportunities", fetch),
            patch.object(latency_arb_service, "_store_opportunities", MagicMock()),
            patch.object(latency_arb_service, "_log_latency_distribution", MagicMock()),
            patch.object(latency_arb_service, "_execute_for_users", AsyncMock(return_value=0)),
            patch.object(latency_arb_service, "_settle_expired_trades", AsyncMock(return_value=0)),
        ):
            await _engine_cycle()
        refresh.assert_not_awaited()


class EngineLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_runs_until_stopped(self):
        stop_event = asyncio.Event()

        async def _cycle():
            stop_event.set()

        heartbeat = MagicMock()
        with (
            patch.object(latency_arb_service, "_engine_cycle", AsyncMock(side_effect=_cycle)),
            patch.object(latency_arb_service, "scheduler_heartbeat", heartbeat),
            patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()),
        ):
            await _engine_loop(stop_event)
        heartbeat.assert_called_once_with("latency_arb")

    async def test_loop_survives_cycle_error(self):
        stop_event = asyncio.Event()
        calls = {"n": 0}

        async def _cycle():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            stop_event.set()

        heartbeat = MagicMock()
        with (
            patch.object(latency_arb_service, "_engine_cycle", AsyncMock(side_effect=_cycle)),
            patch.object(latency_arb_service, "scheduler_heartbeat", heartbeat),
            patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()),
            self.assertLogs(latency_arb_service.logger, level="WARNING"),
        ):
            await _engine_loop(stop_event)
        self.assertEqual(calls["n"], 2)
        # One heartbeat per completed cycle iteration.
        self.assertEqual(heartbeat.call_count, 2)
        heartbeat.assert_called_with("latency_arb")


class EngineLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = (
            latency_arb_service._engine_task,
            latency_arb_service._stop_event,
            latency_arb_service._running,
        )
        latency_arb_service._engine_task = None
        latency_arb_service._stop_event = None
        latency_arb_service._running = False

    def tearDown(self):
        (
            latency_arb_service._engine_task,
            latency_arb_service._stop_event,
            latency_arb_service._running,
        ) = self._saved

    async def test_start_engine(self):
        feed = AsyncMock()
        markets = AsyncMock()
        heartbeat = MagicMock()
        with (
            patch.object(
                latency_arb_service, "acquire_scheduler_lock", MagicMock(return_value=True)
            ),
            patch.object(latency_arb_service.binance_ws_service, "start_binance_feed", feed),
            patch.object(
                latency_arb_service.crypto_markets_service, "start_crypto_markets_refresh", markets
            ),
            patch.object(latency_arb_service, "_engine_cycle", AsyncMock()),
            patch.object(latency_arb_service, "scheduler_heartbeat", heartbeat),
            patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()),
            self.assertLogs(latency_arb_service.logger, level="INFO"),
        ):
            await start_latency_arb_engine()
        self.assertTrue(latency_arb_service._running)
        self.assertIsInstance(latency_arb_service._engine_task, asyncio.Task)
        feed.assert_awaited_once()
        markets.assert_awaited_once()
        await stop_latency_arb_engine()

    async def test_start_engine_when_already_running(self):
        latency_arb_service._running = True
        lock = MagicMock(return_value=True)
        with patch.object(latency_arb_service, "acquire_scheduler_lock", lock):
            await start_latency_arb_engine()
        lock.assert_not_called()

    async def test_start_engine_when_lock_held_elsewhere(self):
        with (
            patch.object(
                latency_arb_service,
                "acquire_scheduler_lock",
                MagicMock(return_value=False),
            ),
            self.assertLogs(latency_arb_service.logger, level="INFO"),
        ):
            await start_latency_arb_engine()
        self.assertFalse(latency_arb_service._running)
        self.assertIsNone(latency_arb_service._engine_task)

    async def test_stop_engine_when_not_running(self):
        latency_arb_service._running = False
        release = MagicMock()
        with patch.object(latency_arb_service, "release_scheduler_lock", release):
            await stop_latency_arb_engine()
        release.assert_not_called()

    async def test_stop_engine_cancels_task_and_releases_lock(self):
        feed = AsyncMock()
        markets = AsyncMock()
        stop_feed = AsyncMock()
        stop_markets = AsyncMock()
        release = MagicMock()
        with (
            patch.object(
                latency_arb_service, "acquire_scheduler_lock", MagicMock(return_value=True)
            ),
            patch.object(latency_arb_service.binance_ws_service, "start_binance_feed", feed),
            patch.object(
                latency_arb_service.crypto_markets_service, "start_crypto_markets_refresh", markets
            ),
            patch.object(latency_arb_service, "_engine_cycle", AsyncMock()),
            patch.object(latency_arb_service, "scheduler_heartbeat", MagicMock()),
            patch.object(latency_arb_service, "get_settings", lambda: _engine_settings()),
        ):
            await start_latency_arb_engine()
        task = latency_arb_service._engine_task
        self.assertIsNotNone(task)
        with (
            patch.object(
                latency_arb_service.crypto_markets_service,
                "stop_crypto_markets_refresh",
                stop_markets,
            ),
            patch.object(latency_arb_service.binance_ws_service, "stop_binance_feed", stop_feed),
            patch.object(latency_arb_service, "release_scheduler_lock", release),
            self.assertLogs(latency_arb_service.logger, level="INFO"),
        ):
            await stop_latency_arb_engine()
        self.assertFalse(latency_arb_service._running)
        self.assertIsNone(latency_arb_service._engine_task)
        self.assertIsNone(latency_arb_service._stop_event)
        self.assertTrue(task.cancelled() or task.done())
        stop_markets.assert_awaited_once()
        stop_feed.assert_awaited_once()
        release.assert_called_once_with("latency_arb")

    async def test_stop_engine_logs_task_error(self):
        async def _stubborn():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise RuntimeError("stubborn task") from None

        task = asyncio.create_task(_stubborn())
        await asyncio.sleep(0)  # let the task start
        latency_arb_service._running = True
        latency_arb_service._engine_task = task
        latency_arb_service._stop_event = asyncio.Event()
        latency_arb_service._stop_event.set()
        stop_markets = AsyncMock()
        stop_feed = AsyncMock()
        release = MagicMock()
        with (
            patch.object(
                latency_arb_service.crypto_markets_service,
                "stop_crypto_markets_refresh",
                stop_markets,
            ),
            patch.object(latency_arb_service.binance_ws_service, "stop_binance_feed", stop_feed),
            patch.object(latency_arb_service, "release_scheduler_lock", release),
            self.assertLogs(latency_arb_service.logger, level="DEBUG"),
        ):
            await stop_latency_arb_engine()
        self.assertFalse(latency_arb_service._running)
        stop_markets.assert_awaited_once()
        stop_feed.assert_awaited_once()
        release.assert_called_once_with("latency_arb")


if __name__ == "__main__":
    unittest.main()
