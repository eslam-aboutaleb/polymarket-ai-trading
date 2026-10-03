"""Coverage tests for the FastAPI application factory (``app.main``).

The lifespan startup wires a dozen background monitors, so every
external collaborator (database session, monitor start/stop
functions, scheduler validation) is patched out — no real
connections are made. Route and middleware behaviour is exercised
through ``TestClient`` without entering the lifespan context.

Covered:

* ``_validate_startup_security`` -- happy path, local-environment
  warning path and the non-local ``RuntimeError`` path.
* ``_validate_database_connectivity`` -- success and failure.
* ``_sync_admin_wallets`` -- empty-list guard, empty-allowed mode,
  invalid wallet filtering, promote/demote with audit records,
  and the rollback-and-reraise failure path.
* ``lifespan`` -- full startup/shutdown with every monitor mocked,
  followed-wallet loading, and the startup-failure path that
  logs a warning and still yields.
* Middleware wiring -- security headers (incl. the docs-path CSP
  exemption), CORS/SlowAPI/request-logger registration, rate
  limiter state and the ``RateLimitExceeded`` handler.
* Routes -- root endpoint, mounted routers (incl. the debug
  router active in the test environment), 404 logging.
* ``general_exception_handler`` -- local detail vs masked detail.
"""

import asyncio
import importlib
import json
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request

import app.main as main_mod
from app.config import get_settings
from app.main import (
    _sync_admin_wallets,
    _validate_database_connectivity,
    _validate_startup_security,
    app,
    general_exception_handler,
    lifespan,
)
from app.middleware.request_logger import (
    RequestLogMiddleware,
    clear_logs,
    get_log_entries,
)
from app.models.followed_trader import FollowedTrader
from app.security.crypto import EncryptionConfigError
from app.security.rate_limit import limiter

ADMIN_WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20


def _http_scope(path="/x", method="GET"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }


class StartupValidationTests(unittest.TestCase):
    def test_security_validation_passes_when_keyring_ok(self):
        with patch("app.main.get_fernet_keyring", MagicMock(return_value=object())):
            _validate_startup_security()  # must not raise

    def test_security_validation_warns_in_local_environment(self):
        with (
            patch(
                "app.main.get_fernet_keyring",
                MagicMock(side_effect=EncryptionConfigError("no keys")),
            ),
            patch("app.main.settings", MagicMock(is_local_environment=True)),
        ):
            self.assertIsNone(_validate_startup_security())

    def test_security_validation_raises_outside_local(self):
        with (
            patch(
                "app.main.get_fernet_keyring",
                MagicMock(side_effect=EncryptionConfigError("no keys")),
            ),
            patch("app.main.settings", MagicMock(is_local_environment=False)),
            self.assertRaises(RuntimeError),
        ):
            _validate_startup_security()

    def test_database_connectivity_passes(self):
        engine = MagicMock()
        with patch("app.main.engine", engine):
            _validate_database_connectivity()  # must not raise
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.assert_called_once()

    def test_database_connectivity_failure_raises(self):
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError("down")
        with patch("app.main.engine", engine), self.assertRaises(RuntimeError):
            _validate_database_connectivity()


class SyncAdminWalletsTests(unittest.TestCase):
    def _patch_settings(self, admin_wallets, allow_empty=False):
        return patch(
            "app.main.settings",
            MagicMock(
                admin_wallets=admin_wallets,
                admin_sync_allow_empty=allow_empty,
            ),
        )

    def test_empty_admin_wallets_skips_sync_by_default(self):
        session_local = MagicMock()
        with self._patch_settings(""), patch("app.main.SessionLocal", session_local):
            _sync_admin_wallets()
        session_local.assert_not_called()

    def test_empty_admin_wallets_allowed_proceeds(self):
        db = MagicMock()
        db.query.return_value.all.return_value = []
        session_local = MagicMock(return_value=db)
        with (
            self._patch_settings("", allow_empty=True),
            patch("app.main.SessionLocal", session_local),
        ):
            _sync_admin_wallets()
        db.commit.assert_called_once()
        db.close.assert_called_once()

    def test_invalid_wallet_entries_are_ignored(self):
        db = MagicMock()
        # A user whose wallet equals the garbage entry must NOT be
        # promoted: the invalid entry never enters the admin set.
        garbage_user = SimpleNamespace(wallet_address="not-a-wallet", is_admin=False)
        valid_user = SimpleNamespace(wallet_address=ADMIN_WALLET, is_admin=False)
        db.query.return_value.all.return_value = [garbage_user, valid_user]
        audit_logs = []

        class FakeAuditLog:
            def __init__(self, **kwargs):
                audit_logs.append(kwargs)

        session_local = MagicMock(return_value=db)
        with (
            self._patch_settings(f"garbage, {ADMIN_WALLET}"),
            patch("app.main.SessionLocal", session_local),
            patch("app.models.audit_log.AdminAuditLog", FakeAuditLog),
        ):
            _sync_admin_wallets()

        self.assertEqual(len(audit_logs), 1)
        self.assertEqual(audit_logs[0]["action"], "promoted")
        self.assertEqual(audit_logs[0]["wallet_address"], ADMIN_WALLET)
        self.assertFalse(garbage_user.is_admin)
        self.assertTrue(valid_user.is_admin)

    def test_promote_and_demote_with_audit_log(self):
        db = MagicMock()
        to_promote = SimpleNamespace(wallet_address=ADMIN_WALLET.upper(), is_admin=False)
        to_demote = SimpleNamespace(wallet_address=OTHER_WALLET, is_admin=True)
        db.query.return_value.all.return_value = [to_promote, to_demote]
        audit_logs = []

        class FakeAuditLog:
            def __init__(self, **kwargs):
                audit_logs.append(kwargs)

        session_local = MagicMock(return_value=db)
        with (
            self._patch_settings(ADMIN_WALLET),
            patch("app.main.SessionLocal", session_local),
            patch("app.models.audit_log.AdminAuditLog", FakeAuditLog),
        ):
            _sync_admin_wallets()

        self.assertEqual(len(audit_logs), 2)
        promoted = audit_logs[0]
        self.assertEqual(promoted["action"], "promoted")
        self.assertEqual(promoted["source"], "env_sync")
        self.assertEqual(promoted["previous_state"], False)
        self.assertEqual(promoted["new_state"], True)
        demoted = audit_logs[1]
        self.assertEqual(demoted["action"], "demoted")
        self.assertEqual(demoted["previous_state"], True)
        self.assertEqual(demoted["new_state"], False)
        self.assertEqual(db.add.call_count, 2)
        db.commit.assert_called_once()
        db.close.assert_called_once()

    def test_no_changes_when_states_match(self):
        db = MagicMock()
        already_admin = SimpleNamespace(wallet_address=ADMIN_WALLET, is_admin=True)
        db.query.return_value.all.return_value = [already_admin]
        session_local = MagicMock(return_value=db)
        with (
            self._patch_settings(ADMIN_WALLET),
            patch("app.main.SessionLocal", session_local),
            patch("app.models.audit_log.AdminAuditLog", MagicMock()),
        ):
            _sync_admin_wallets()
        db.add.assert_not_called()
        db.commit.assert_called_once()

    def test_database_failure_rolls_back_and_reraises(self):
        db = MagicMock()
        db.query.side_effect = RuntimeError("db down")
        session_local = MagicMock(return_value=db)
        with (
            self._patch_settings(ADMIN_WALLET),
            patch("app.main.SessionLocal", session_local),
            self.assertRaises(RuntimeError),
        ):
            _sync_admin_wallets()
        db.rollback.assert_called_once()
        db.close.assert_called_once()


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    """Runs the real lifespan with every monitor mocked out."""

    START_TARGETS = [
        ("start_trade_monitor", "app.services.trade_monitor", True),
        ("start_clob_ws_manager", "app.services.clob_ws_manager", False),
        ("refresh_leaderboard_background", "app.services.leaderboard_service", True),
        ("start_stop_loss_monitor", "app.services.stop_loss_monitor", True),
        ("start_inverse_bot_monitor", "app.services.inverse_bot_monitor", True),
        (
            "start_all_enabled_market_makers",
            "app.services.market_maker_service",
            True,
        ),
        (
            "start_position_lifecycle_manager",
            "app.services.position_lifecycle_service",
            True,
        ),
        ("start_aggregation_service", "app.services.trade_aggregation_service", True),
        ("start_arbitrage_monitor", "app.services.arbitrage_service", True),
        ("start_news_generator", "app.services.news_service", True),
        ("start_ctf_events_monitor", "app.services.ctf_events_service", True),
        ("start_redemption_manager", "app.services.redemption_service", True),
        ("start_latency_arb_engine", "app.services.latency_arb_service", True),
        ("add_watched_wallet", "app.services.trade_monitor", False),
    ]

    STOP_TARGETS = [
        ("stop_trade_monitor", "app.services.trade_monitor"),
        ("stop_clob_ws_manager", "app.services.clob_ws_manager"),
        ("stop_stop_loss_monitor", "app.services.stop_loss_monitor"),
        ("stop_inverse_bot_monitor", "app.services.inverse_bot_monitor"),
        ("stop_all_market_makers", "app.services.market_maker_service"),
        (
            "stop_position_lifecycle_manager",
            "app.services.position_lifecycle_service",
        ),
        ("stop_aggregation_service", "app.services.trade_aggregation_service"),
        ("stop_arbitrage_monitor", "app.services.arbitrage_service"),
        ("stop_news_generator", "app.services.news_service"),
        ("stop_ctf_events_monitor", "app.services.ctf_events_service"),
        ("stop_redemption_manager", "app.services.redemption_service"),
        ("stop_latency_arb_engine", "app.services.latency_arb_service"),
        ("close_shared_channels", "app.grpc_clients.analysis_client"),
    ]

    def _install(self, name, module_path, is_async):
        mock = AsyncMock() if is_async else MagicMock()
        patcher = patch(f"{module_path}.{name}", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    async def asyncTearDown(self):
        if main_mod._background_tasks:
            await asyncio.gather(*main_mod._background_tasks, return_exceptions=True)
            main_mod._background_tasks.clear()

    async def test_lifespan_starts_and_stops_every_service(self):
        starts = {
            name: self._install(name, module_path, is_async)
            for name, module_path, is_async in self.START_TARGETS
        }
        stops = {
            name: self._install(name, module_path, True) for name, module_path in self.STOP_TARGETS
        }
        db = MagicMock()
        db.query.return_value.filter.return_value.distinct.return_value.all.return_value = []
        validators = [
            patch("app.main._validate_startup_security", MagicMock()),
            patch("app.main._validate_database_connectivity", MagicMock()),
            patch("app.main._sync_admin_wallets", MagicMock()),
            patch("app.main.SessionLocal", MagicMock(return_value=db)),
        ]
        for patcher in validators:
            patcher.start()
            self.addCleanup(patcher.stop)

        async with lifespan(app):
            # Yield so the create_task'd monitor/leaderboard
            # coroutines actually execute and register awaits.
            await asyncio.sleep(0)

        for name, mock in starts.items():
            if name == "add_watched_wallet":
                mock.assert_not_called()
            elif name == "start_clob_ws_manager":
                mock.assert_called_once()
            else:
                mock.assert_awaited_once()
        starts["refresh_leaderboard_background"].assert_awaited_once_with(db=None, interval=300)
        for mock in stops.values():
            mock.assert_awaited_once_with()

        # Background tasks (monitor, leaderboard, watchdog) were
        # created, ran, and then cancelled at shutdown.
        await asyncio.gather(*main_mod._background_tasks, return_exceptions=True)
        self.assertTrue(all(task.done() for task in main_mod._background_tasks))

    async def test_lifespan_loads_followed_wallets(self):
        starts = {
            name: self._install(name, module_path, is_async)
            for name, module_path, is_async in self.START_TARGETS
        }
        for name, module_path in self.STOP_TARGETS:
            self._install(name, module_path, True)

        followed = SimpleNamespace(trader_wallet="0xABCDEF")
        notif = SimpleNamespace(trader_wallet="0x123456")
        empty_wallet = SimpleNamespace(trader_wallet="")

        def query_side_effect(entity, *args, **kwargs):
            result = MagicMock()
            if entity.class_ is FollowedTrader:
                result.filter.return_value.distinct.return_value.all.return_value = [
                    followed,
                    None,
                ]
            else:
                result.filter.return_value.distinct.return_value.all.return_value = [
                    notif,
                    empty_wallet,
                ]
            return result

        db = MagicMock()
        db.query.side_effect = query_side_effect

        validators = [
            patch("app.main._validate_startup_security", MagicMock()),
            patch("app.main._validate_database_connectivity", MagicMock()),
            patch("app.main._sync_admin_wallets", MagicMock()),
            patch("app.main.SessionLocal", MagicMock(return_value=db)),
        ]
        for patcher in validators:
            patcher.start()
            self.addCleanup(patcher.stop)

        async with lifespan(app):
            pass

        add_watched = starts["add_watched_wallet"]
        self.assertEqual(add_watched.call_count, 2)
        add_watched.assert_any_call("0xabcdef")
        add_watched.assert_any_call("0x123456")

    async def test_lifespan_survives_startup_failure(self):
        starts = {
            name: self._install(name, module_path, is_async)
            for name, module_path, is_async in self.START_TARGETS
        }
        stops = {
            name: self._install(name, module_path, True) for name, module_path in self.STOP_TARGETS
        }
        # A directly-awaited monitor blows up during startup.
        starts["start_stop_loss_monitor"].side_effect = RuntimeError("monitor down")

        validators = [
            patch("app.main._validate_startup_security", MagicMock()),
            patch("app.main._validate_database_connectivity", MagicMock()),
            patch("app.main._sync_admin_wallets", MagicMock()),
            patch("app.main.SessionLocal", MagicMock()),
        ]
        for patcher in validators:
            patcher.start()
            self.addCleanup(patcher.stop)

        async with lifespan(app):
            # The lifespan still yields despite the failure.
            await asyncio.sleep(0)

        starts["start_trade_monitor"].assert_awaited_once()
        starts["start_clob_ws_manager"].assert_called_once()
        starts["start_stop_loss_monitor"].assert_awaited_once()
        # Services started after the failure never ran.
        starts["start_inverse_bot_monitor"].assert_not_awaited()
        starts["start_news_generator"].assert_not_awaited()
        # Shutdown hooks still ran.
        for _name, mock in stops.items():
            mock.assert_awaited_once_with()

        await asyncio.gather(*main_mod._background_tasks, return_exceptions=True)


class AppWiringTests(unittest.TestCase):
    def setUp(self):
        clear_logs()
        self.client = TestClient(app)

    def test_root_endpoint(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["message"], "Welcome to Polymarket AI Trading API")
        self.assertEqual(body["docs"], "/docs")
        self.assertEqual(body["redoc"], "/redoc")

    def test_security_headers_on_api_responses(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "strict-origin-when-cross-origin")
        self.assertEqual(
            response.headers["permissions-policy"],
            "camera=(), microphone=(), geolocation=()",
        )
        self.assertIn("content-security-policy", response.headers)

    def test_docs_paths_skip_content_security_policy(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn("content-security-policy", response.headers, path)

    def test_docs_subpaths_skip_content_security_policy(self):
        response = self.client.get("/docs/oauth2-redirect")
        self.assertNotIn("content-security-policy", response.headers)

    def test_middleware_and_limiter_are_wired(self):
        middleware_classes = [m.cls for m in app.user_middleware]
        self.assertIn(CORSMiddleware, middleware_classes)
        self.assertIn(SlowAPIMiddleware, middleware_classes)
        self.assertIn(RequestLogMiddleware, middleware_classes)
        self.assertIs(app.state.limiter, limiter)
        self.assertIn(RateLimitExceeded, app.exception_handlers)

    def test_routers_are_mounted(self):
        paths = {getattr(route, "path", "") for route in app.routes}
        for expected in (
            "/api/auth/login",
            "/api/auth/trading-key",
            "/api/analysis/market",
            "/api/portfolio/balance",
            "/api/settings",
            "/api/trades/history",
            "/api/markets/categories",
            "/api/inverse-bot/positions",
            "/api/market-maker/configs",
            "/api/backtesting/runs",
            "/api/binance/signals/smart-money",
            "/api/news/feed",
            "/health",
            "/metrics",
            "/api/whales/events",
            "/api/notifications",
            "/api/latency-arb/config",
        ):
            self.assertIn(expected, paths, expected)

    def test_debug_router_mounted_in_test_environment(self):
        # ENVIRONMENT=test makes debug_endpoints_active truthy.
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/api/debug/logs", paths)
        self.assertIn("/api/debug/stats", paths)
        self.assertIn("/api/debug/health", paths)

    def test_requests_are_logged(self):
        self.client.get("/")
        entries = get_log_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["path"], "/")
        self.assertEqual(entry["status_code"], 200)
        self.assertEqual(entry["level"], "info")
        self.assertIn("request_id", entry)
        self.assertIn("timestamp", entry)
        self.assertIn("duration_ms", entry)

    def test_404_is_logged_as_warning(self):
        self.client.get("/no-such-route")
        entry = get_log_entries()[0]
        self.assertEqual(entry["status_code"], 404)
        self.assertEqual(entry["level"], "warning")
        self.assertEqual(entry["error_detail"], "client_error")

    def test_docs_and_health_are_not_logged(self):
        self.client.get("/docs")
        self.client.get("/openapi.json")
        self.assertEqual(len(get_log_entries()), 0)


class GeneralExceptionHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_environment_returns_exception_detail(self):
        request = Request(_http_scope("/boom"))
        response = await general_exception_handler(request, ValueError("boom"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(json.loads(response.body), {"detail": "boom"})

    async def test_non_local_environment_masks_detail(self):
        request = Request(_http_scope("/boom"))
        with patch("app.main.settings", MagicMock(is_local_environment=False)):
            response = await general_exception_handler(request, ValueError("boom"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(json.loads(response.body), {"detail": "Internal server error"})


class SchedulerWatchdogTests(LifespanTests):
    """Exercises the dead-man's-switch watchdog closure.

    The watchdog sleeps 60s per iteration, so ``asyncio.sleep``
    is replaced with a fake that walks a list of per-iteration
    heartbeat-age scenarios and then cancels the task (mirroring
    the lifespan shutdown cancelling it).
    """

    async def _run_watchdog(self, scenarios, dispatch_mock, alert_module=None):
        for name, module_path, is_async in self.START_TARGETS:
            self._install(name, module_path, is_async)
        for name, module_path in self.STOP_TARGETS:
            self._install(name, module_path, True)

        state = {"idx": 0}
        current = {"scenario": {}}
        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            if delay == 0:
                await real_sleep(0)
                return
            if state["idx"] < len(scenarios):
                current["scenario"] = scenarios[state["idx"]]
                state["idx"] += 1
                await real_sleep(0)
            else:
                raise asyncio.CancelledError()

        def fake_age(name):
            return current["scenario"].get(name)

        validators = [
            patch("app.main._validate_startup_security", MagicMock()),
            patch("app.main._validate_database_connectivity", MagicMock()),
            patch("app.main._sync_admin_wallets", MagicMock()),
            patch("app.main.SessionLocal", MagicMock()),
        ]
        for patcher in validators:
            patcher.start()
            self.addCleanup(patcher.stop)

        patches = [
            patch("asyncio.sleep", fake_sleep),
            patch("app.utils.scheduler_lock.scheduler_heartbeat_age", fake_age),
            patch(
                "app.utils.scheduler_lock.scheduler_stale_threshold",
                lambda name: 100,
            ),
            patch("app.services.alert_service.dispatch", dispatch_mock),
        ]
        if alert_module is not None:
            patches.append(patch.dict(sys.modules, {"app.services.alert_service": alert_module}))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        async with lifespan(app):
            await asyncio.sleep(0)
            watchdog = next(
                task
                for task in main_mod._background_tasks
                if task.get_coro().__name__ == "_scheduler_watchdog"
            )
            with self.assertRaises(asyncio.CancelledError):
                await watchdog

    async def test_watchdog_skips_missing_and_fresh_heartbeats_then_alerts(self):
        scenarios = [
            {},  # every scheduler never seen -> age None -> continue
            {"stop_loss_monitor": 5},  # fresh heartbeat -> age <= threshold
            {"stop_loss_monitor": 9999},  # stale -> alert dispatched
            {"stop_loss_monitor": 9999},  # stale -> dispatch raises -> warning
        ]
        dispatch_mock = AsyncMock(side_effect=[None, RuntimeError("alert down")])
        await self._run_watchdog(scenarios, dispatch_mock)

        # Once for the successful dispatch, once for the
        # dispatch that raised (its failure is only logged).
        self.assertEqual(dispatch_mock.await_count, 2)
        dispatch_mock.assert_any_await(
            "dead_man_switch",
            None,
            {
                "scheduler": "stop_loss_monitor",
                "heartbeat_age_seconds": 9999,
                "threshold_seconds": 100,
            },
        )

    async def test_watchdog_swallows_alert_import_error(self):
        scenarios = [{"stop_loss_monitor": 9999}]
        dispatch_mock = AsyncMock()
        bare_module = types.ModuleType("app.services.alert_service")
        await self._run_watchdog(scenarios, dispatch_mock, alert_module=bare_module)
        dispatch_mock.assert_not_awaited()

    async def test_shutdown_hook_failure_does_not_block_remaining_hooks(self):
        for name, module_path, is_async in self.START_TARGETS:
            self._install(name, module_path, is_async)
        stops = {}
        for name, module_path in self.STOP_TARGETS:
            mock = AsyncMock()
            stops[name] = mock
            patcher = patch(f"{module_path}.{name}", mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        stops["stop_trade_monitor"].side_effect = RuntimeError("stop failed")
        stops["stop_latency_arb_engine"].side_effect = ValueError("also failed")

        validators = [
            patch("app.main._validate_startup_security", MagicMock()),
            patch("app.main._validate_database_connectivity", MagicMock()),
            patch("app.main._sync_admin_wallets", MagicMock()),
            patch("app.main.SessionLocal", MagicMock()),
        ]
        for patcher in validators:
            patcher.start()
            self.addCleanup(patcher.stop)

        async with lifespan(app):
            await asyncio.sleep(0)

        # Every shutdown hook ran exactly once, including the
        # two that raised (their failures are logged, not raised).
        for mock in stops.values():
            mock.assert_awaited_once_with()


class DebugRouterToggleTests(unittest.TestCase):
    def test_debug_router_disabled_outside_local_environment(self):
        env = {"ENVIRONMENT": "production", "DEBUG_ENDPOINTS_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            get_settings.cache_clear()
            try:
                reloaded = importlib.reload(main_mod)
                paths = {getattr(route, "path", "") for route in reloaded.app.routes}
                self.assertNotIn("/api/debug/logs", paths)
                self.assertNotIn("/api/debug/stats", paths)
                self.assertNotIn("/api/debug/health", paths)
            finally:
                get_settings.cache_clear()
                importlib.reload(main_mod)


if __name__ == "__main__":
    unittest.main()
