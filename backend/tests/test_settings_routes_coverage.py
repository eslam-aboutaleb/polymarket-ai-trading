"""Coverage tests for ``app.api.routes.settings``.

Every external boundary is replaced with a double:

* the DB session is a ``FakeDB`` returning configured rows (the same
  chainable-query pattern used by ``test_copy_trade_service_coverage.py``),
* the LLM gateway singleton is patched in the ``settings`` route namespace,
* the gRPC ``AnalysisClient`` is patched on its source module so no
  channel is ever opened,
* the paper-summary service function is patched on its source module,
* ``os.environ`` is isolated with ``patch.dict`` where routes read it.

No network, database or gRPC service is touched.
"""

import os
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.auth import get_current_user_from_token
from app.main import app
from app.models.user import User
from app.models.user_settings import UserSettings
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20
ADMIN_WALLET = "0x" + "cd" * 20

NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
COOLDOWN = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ────────────── Test doubles ──────────────


class FakeQuery:
    """Chainable stand-in for a SQLAlchemy query.

    ``filter`` records the literal values of the filter criteria
    so tests can configure per-criteria results (e.g. a user
    lookup that finds one id but not another).
    """

    def __init__(self, db, key, filter_values=()):
        self.db = db
        self.key = key
        self.filter_values = tuple(filter_values)

    def filter(self, *args, **kwargs):
        values = []
        for arg in args:
            right = getattr(arg, "right", None)
            values.append(getattr(right, "value", None))
        return FakeQuery(self.db, self.key, self.filter_values + tuple(values))

    def outerjoin(self, *args, **kwargs):
        return self

    def offset(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def first(self):
        specific = (self.key, self.filter_values)
        if specific in self.db.filter_results:
            return self.db.filter_results[specific]
        result = self.db.first_results.get(self.key)
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def all(self):
        result = self.db.all_results.get(self.key)
        if result is not None:
            return result
        fallback = self.db.first_results.get(self.key)
        if isinstance(fallback, list):
            return fallback
        return []

    def scalar(self):
        return self.db.scalar_result


class FakeDB:
    """In-memory stand-in for a SQLAlchemy session."""

    def __init__(self):
        self.first_results = {}
        self.filter_results = {}
        self.all_results = {}
        self.scalar_result = 0
        self.added = []
        self.flushed = False
        self.committed = False
        self.refreshed = []

    @staticmethod
    def _key(*entities):
        parts = []
        for entity in entities:
            if isinstance(entity, type):
                parts.append(entity.__name__)
            else:
                parts.append(str(entity))
        return tuple(parts)

    def query(self, *entities):
        return FakeQuery(self, self._key(*entities))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed = True

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        self.refreshed.append(obj)


def make_settings(**overrides):
    """Build a transient ``UserSettings`` with sensible defaults."""
    kwargs = {
        "user_id": 1,
        "ai_backend": "llm_chain",
        "preferred_llm_provider": None,
        "preferred_llm_model": None,
        "copy_trading_enabled": False,
        "risk_mode": "max_position_daily_loss",
        "max_position_size": 100.0,
        "daily_loss_limit": 500.0,
        "mirror_percentage": 10.0,
        "fixed_trade_amount": 50.0,
        "kelly_fraction": 0.25,
        "require_ai_approval": True,
        "follow_email_notifications_enabled": False,
        "monthly_loss_limit": None,
        "max_drawdown_pct": 25.0,
        "total_loss_halt_pct": 40.0,
        "peak_capital": None,
        "initial_capital": None,
        "trading_halted": False,
        "halt_reason": None,
        "cooldown_until": None,
        "dynamic_sizing_enabled": False,
        "consecutive_wins": 0,
        "consecutive_losses": 0,
        "simulation_mode": False,
        "paper_balance": 1000.0,
        "inverse_bot_enabled": False,
        "inverse_bot_default_size_mode": "full_notional",
        "inverse_bot_fixed_amount": 50.0,
        "inverse_bot_confidence_threshold": 75,
        "inverse_bot_cooldown_minutes": 30,
        "inverse_bot_max_reversals_per_day": 3,
        "updated_at": None,
    }
    kwargs.update(overrides)
    return UserSettings(**kwargs)


def make_user(**overrides):
    """Build a transient ``User`` with sensible defaults."""
    kwargs = {
        "id": 1,
        "wallet_address": WALLET,
        "display_name": None,
        "email": None,
        "phone": None,
        "profile_picture_url": None,
        "is_admin": False,
    }
    kwargs.update(overrides)
    return User(**kwargs)


class SettingsRouteTestCase(unittest.TestCase):
    """Base class wiring the fake DB and auth override into the app."""

    def setUp(self):
        self.db = FakeDB()
        app.dependency_overrides[get_db] = lambda: self.db
        self._authenticate()
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)

    def _authenticate(self, user_id=1, wallet=WALLET, is_admin=False):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": user_id,
            "wallet_address": wallet,
            "is_admin": is_admin,
        }

    def _make_admin(self, user_id=1, wallet=ADMIN_WALLET):
        """Authenticate as an admin and seed the matching user row."""
        self._authenticate(user_id=user_id, wallet=wallet, is_admin=True)
        admin = make_user(id=user_id, wallet_address=wallet, is_admin=True)
        self.db.first_results[("User",)] = admin
        return admin

    def _patch_gateway(self, providers):
        """Patch ``get_llm_gateway`` in the settings route namespace."""
        gateway = MagicMock()
        gateway.list_providers.return_value = providers
        return patch("app.api.routes.settings.get_llm_gateway", return_value=gateway)

    def _patch_analysis_clients(self, llm_health, cli_health):
        """Patch ``AnalysisClient`` so each backend gets its own mock.

        ``llm_health`` / ``cli_health`` are either a bool (returned by
        ``health_check``) or an exception instance (raised by it).
        """
        from app.config import AIBackend

        def make_client(health):
            client = AsyncMock()
            if isinstance(health, Exception):
                client.health_check = AsyncMock(side_effect=health)
            else:
                client.health_check = AsyncMock(return_value=health)
            client.close = AsyncMock()
            return client

        llm_client = make_client(llm_health)
        cli_client = make_client(cli_health)

        def factory(backend=None):
            if backend == AIBackend.LLM_CHAIN:
                return llm_client
            return cli_client

        return patch(
            "app.grpc_clients.analysis_client.AnalysisClient",
            side_effect=factory,
        )


# ── GET /api/settings ───────────────────────────────────


class GetUserSettingsTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 401)

    def test_no_record_returns_defaults(self):
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ai_backend"], "llm_chain")
        self.assertIsNone(data["preferred_llm_provider"])
        self.assertIsNone(data["preferred_llm_model"])
        self.assertIsNone(data["updated_at"])
        self.assertFalse(data["copy_trading_enabled"])
        self.assertEqual(data["risk_mode"], "max_position_daily_loss")
        self.assertEqual(data["kelly_fraction"], 0.25)
        self.assertEqual(data["paper_balance"], 1000.0)
        self.assertEqual(data["inverse_bot_default_size_mode"], "full_notional")

    def test_full_record_is_serialized(self):
        self.db.first_results[("UserSettings",)] = make_settings(
            ai_backend="cli_agent",
            preferred_llm_provider="anthropic",
            preferred_llm_model="claude-sonnet-4",
            copy_trading_enabled=True,
            risk_mode="percentage_mirror",
            max_position_size=50.0,
            daily_loss_limit=250.0,
            mirror_percentage=25.0,
            fixed_trade_amount=75.0,
            kelly_fraction=0.5,
            require_ai_approval=False,
            follow_email_notifications_enabled=True,
            monthly_loss_limit=1000.0,
            max_drawdown_pct=15.0,
            total_loss_halt_pct=35.0,
            peak_capital=5000.0,
            initial_capital=2000.0,
            trading_halted=True,
            halt_reason="drawdown",
            cooldown_until=COOLDOWN,
            dynamic_sizing_enabled=True,
            consecutive_wins=3,
            consecutive_losses=2,
            simulation_mode=True,
            paper_balance=2000.0,
            inverse_bot_enabled=True,
            inverse_bot_default_size_mode="fixed_amount",
            inverse_bot_fixed_amount=25.0,
            inverse_bot_confidence_threshold=60,
            inverse_bot_cooldown_minutes=15,
            inverse_bot_max_reversals_per_day=5,
            updated_at=NOW,
        )
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ai_backend"], "cli_agent")
        self.assertEqual(data["preferred_llm_provider"], "anthropic")
        self.assertEqual(data["preferred_llm_model"], "claude-sonnet-4")
        self.assertTrue(data["copy_trading_enabled"])
        self.assertEqual(data["risk_mode"], "percentage_mirror")
        self.assertEqual(data["max_position_size"], 50.0)
        self.assertEqual(data["daily_loss_limit"], 250.0)
        self.assertEqual(data["mirror_percentage"], 25.0)
        self.assertEqual(data["fixed_trade_amount"], 75.0)
        self.assertEqual(data["kelly_fraction"], 0.5)
        self.assertFalse(data["require_ai_approval"])
        self.assertTrue(data["follow_email_notifications_enabled"])
        self.assertEqual(data["monthly_loss_limit"], 1000.0)
        self.assertEqual(data["max_drawdown_pct"], 15.0)
        self.assertEqual(data["total_loss_halt_pct"], 35.0)
        self.assertEqual(data["peak_capital"], 5000.0)
        self.assertEqual(data["initial_capital"], 2000.0)
        self.assertTrue(data["trading_halted"])
        self.assertEqual(data["halt_reason"], "drawdown")
        self.assertEqual(data["cooldown_until"], COOLDOWN.isoformat())
        self.assertTrue(data["dynamic_sizing_enabled"])
        self.assertEqual(data["consecutive_wins"], 3)
        self.assertEqual(data["consecutive_losses"], 2)
        self.assertTrue(data["simulation_mode"])
        self.assertEqual(data["paper_balance"], 2000.0)
        self.assertTrue(data["inverse_bot_enabled"])
        self.assertEqual(data["inverse_bot_default_size_mode"], "fixed_amount")
        self.assertEqual(data["inverse_bot_fixed_amount"], 25.0)
        self.assertEqual(data["inverse_bot_confidence_threshold"], 60)
        self.assertEqual(data["inverse_bot_cooldown_minutes"], 15)
        self.assertEqual(data["inverse_bot_max_reversals_per_day"], 5)
        self.assertEqual(data["updated_at"], NOW.isoformat())

    def test_null_fields_fall_back_to_defaults(self):
        self.db.first_results[("UserSettings",)] = make_settings(
            copy_trading_enabled=None,
            risk_mode=None,
            max_position_size=None,
            daily_loss_limit=None,
            mirror_percentage=None,
            fixed_trade_amount=None,
            kelly_fraction=None,
            require_ai_approval=None,
            follow_email_notifications_enabled=None,
            max_drawdown_pct=None,
            total_loss_halt_pct=None,
            trading_halted=None,
            dynamic_sizing_enabled=None,
            consecutive_wins=None,
            consecutive_losses=None,
            simulation_mode=None,
            paper_balance=None,
            inverse_bot_enabled=None,
            inverse_bot_default_size_mode=None,
            inverse_bot_fixed_amount=None,
            inverse_bot_confidence_threshold=None,
            inverse_bot_cooldown_minutes=None,
            inverse_bot_max_reversals_per_day=None,
        )
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["copy_trading_enabled"])
        self.assertEqual(data["risk_mode"], "max_position_daily_loss")
        self.assertEqual(data["max_position_size"], 100.0)
        self.assertEqual(data["daily_loss_limit"], 500.0)
        self.assertEqual(data["mirror_percentage"], 10.0)
        self.assertEqual(data["fixed_trade_amount"], 50.0)
        self.assertEqual(data["kelly_fraction"], 0.25)
        self.assertTrue(data["require_ai_approval"])
        self.assertFalse(data["follow_email_notifications_enabled"])
        self.assertEqual(data["max_drawdown_pct"], 25.0)
        self.assertEqual(data["total_loss_halt_pct"], 40.0)
        self.assertFalse(data["trading_halted"])
        self.assertFalse(data["dynamic_sizing_enabled"])
        self.assertEqual(data["consecutive_wins"], 0)
        self.assertEqual(data["consecutive_losses"], 0)
        self.assertFalse(data["simulation_mode"])
        self.assertEqual(data["paper_balance"], 1000.0)
        self.assertFalse(data["inverse_bot_enabled"])
        self.assertEqual(data["inverse_bot_default_size_mode"], "full_notional")
        self.assertEqual(data["inverse_bot_fixed_amount"], 50.0)
        self.assertEqual(data["inverse_bot_confidence_threshold"], 75)
        self.assertEqual(data["inverse_bot_cooldown_minutes"], 30)
        self.assertEqual(data["inverse_bot_max_reversals_per_day"], 3)


# ── PUT /api/settings ───────────────────────────────────


class UpdateUserSettingsTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.put("/api/settings", json={})
        self.assertEqual(response.status_code, 401)

    def test_invalid_ai_backend_returns_400(self):
        response = self.client.put("/api/settings", json={"ai_backend": "bogus"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid ai_backend", response.json()["detail"])

    def test_kelly_fraction_out_of_range_returns_422(self):
        response = self.client.put("/api/settings", json={"kelly_fraction": 1.5})
        self.assertEqual(response.status_code, 422)

    def test_creates_new_record(self):
        response = self.client.put(
            "/api/settings", json={"ai_backend": "cli_agent", "kelly_fraction": 0.75}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.db.added), 1)
        created = self.db.added[0]
        self.assertIsInstance(created, UserSettings)
        self.assertEqual(created.user_id, 1)
        self.assertEqual(created.ai_backend, "cli_agent")
        self.assertEqual(created.kelly_fraction, 0.75)
        self.assertTrue(self.db.committed)
        self.assertEqual(response.json()["ai_backend"], "cli_agent")
        self.assertEqual(response.json()["kelly_fraction"], 0.75)

    def test_creates_record_with_defaults_for_empty_body(self):
        response = self.client.put("/api/settings", json={})
        self.assertEqual(response.status_code, 200)
        created = self.db.added[0]
        self.assertEqual(created.ai_backend, "llm_chain")
        self.assertIsNone(created.kelly_fraction)
        self.assertEqual(response.json()["ai_backend"], "llm_chain")
        self.assertEqual(response.json()["kelly_fraction"], 0.25)

    def test_updates_existing_record(self):
        existing = make_settings(ai_backend="llm_chain", kelly_fraction=0.25)
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.put(
            "/api/settings", json={"ai_backend": "cli_agent", "kelly_fraction": 0.9}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.ai_backend, "cli_agent")
        self.assertEqual(existing.kelly_fraction, 0.9)
        self.assertIsNotNone(existing.updated_at)
        self.assertTrue(self.db.committed)
        self.assertIn(existing, self.db.refreshed)
        self.assertEqual(response.json()["ai_backend"], "cli_agent")
        self.assertEqual(response.json()["kelly_fraction"], 0.9)

    def test_updates_kelly_only_when_backend_omitted(self):
        existing = make_settings(ai_backend="llm_chain", kelly_fraction=0.25)
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.put("/api/settings", json={"kelly_fraction": 0.1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.ai_backend, "llm_chain")
        self.assertEqual(existing.kelly_fraction, 0.1)
        self.assertEqual(response.json()["ai_backend"], "llm_chain")
        self.assertEqual(response.json()["kelly_fraction"], 0.1)


# ── /api/settings/copy-trading ──────────────────────────


class CopyTradingSettingsTests(SettingsRouteTestCase):
    def test_get_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/copy-trading")
        self.assertEqual(response.status_code, 401)

    def test_get_delegates_to_get_user_settings(self):
        self.db.first_results[("UserSettings",)] = make_settings(
            copy_trading_enabled=True, risk_mode="fixed_amount"
        )
        response = self.client.get("/api/settings/copy-trading")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["copy_trading_enabled"])
        self.assertEqual(data["risk_mode"], "fixed_amount")

    def test_put_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.put("/api/settings/copy-trading", json={})
        self.assertEqual(response.status_code, 401)

    def test_put_creates_new_record_and_flushes(self):
        response = self.client.put(
            "/api/settings/copy-trading", json={"copy_trading_enabled": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.db.added), 1)
        created = self.db.added[0]
        self.assertEqual(created.user_id, 1)
        self.assertEqual(created.ai_backend, "llm_chain")
        self.assertTrue(created.copy_trading_enabled)
        self.assertTrue(self.db.flushed)
        self.assertTrue(self.db.committed)
        self.assertTrue(response.json()["copy_trading_enabled"])

    def test_put_updates_all_fields(self):
        existing = make_settings(peak_capital=1000.0)
        self.db.first_results[("UserSettings",)] = existing
        payload = {
            "copy_trading_enabled": True,
            "risk_mode": "percentage_mirror",
            "max_position_size": 50.0,
            "daily_loss_limit": 250.0,
            "mirror_percentage": 25.0,
            "fixed_trade_amount": 75.0,
            "kelly_fraction": 0.5,
            "require_ai_approval": False,
            "follow_email_notifications_enabled": True,
            "monthly_loss_limit": 1000.0,
            "max_drawdown_pct": 15.0,
            "total_loss_halt_pct": 35.0,
            "initial_capital": 2000.0,
            "dynamic_sizing_enabled": True,
            "simulation_mode": True,
            "paper_balance": 2000.0,
            "inverse_bot_enabled": True,
            "inverse_bot_default_size_mode": "fixed_amount",
            "inverse_bot_fixed_amount": 25.0,
            "inverse_bot_confidence_threshold": 60,
            "inverse_bot_cooldown_minutes": 15,
            "inverse_bot_max_reversals_per_day": 5,
        }
        response = self.client.put("/api/settings/copy-trading", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["copy_trading_enabled"])
        self.assertEqual(data["risk_mode"], "percentage_mirror")
        self.assertEqual(data["max_position_size"], 50.0)
        self.assertEqual(data["daily_loss_limit"], 250.0)
        self.assertEqual(data["mirror_percentage"], 25.0)
        self.assertEqual(data["fixed_trade_amount"], 75.0)
        self.assertEqual(data["kelly_fraction"], 0.5)
        self.assertFalse(data["require_ai_approval"])
        self.assertTrue(data["follow_email_notifications_enabled"])
        self.assertEqual(data["monthly_loss_limit"], 1000.0)
        self.assertEqual(data["max_drawdown_pct"], 15.0)
        self.assertEqual(data["total_loss_halt_pct"], 35.0)
        self.assertEqual(data["initial_capital"], 2000.0)
        self.assertTrue(data["dynamic_sizing_enabled"])
        self.assertTrue(data["simulation_mode"])
        self.assertEqual(data["paper_balance"], 2000.0)
        self.assertTrue(data["inverse_bot_enabled"])
        self.assertEqual(data["inverse_bot_default_size_mode"], "fixed_amount")
        self.assertEqual(data["inverse_bot_fixed_amount"], 25.0)
        self.assertEqual(data["inverse_bot_confidence_threshold"], 60)
        self.assertEqual(data["inverse_bot_cooldown_minutes"], 15)
        self.assertEqual(data["inverse_bot_max_reversals_per_day"], 5)

    def test_initial_capital_sets_peak_when_peak_is_none(self):
        existing = make_settings(peak_capital=None)
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.put("/api/settings/copy-trading", json={"initial_capital": 3000.0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.initial_capital, 3000.0)
        self.assertEqual(existing.peak_capital, 3000.0)
        self.assertEqual(response.json()["peak_capital"], 3000.0)

    def test_initial_capital_raises_peak_when_lower(self):
        existing = make_settings(peak_capital=1000.0)
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.put("/api/settings/copy-trading", json={"initial_capital": 2000.0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.peak_capital, 2000.0)

    def test_initial_capital_keeps_higher_peak(self):
        existing = make_settings(peak_capital=5000.0)
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.put("/api/settings/copy-trading", json={"initial_capital": 2000.0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.initial_capital, 2000.0)
        self.assertEqual(existing.peak_capital, 5000.0)
        self.assertEqual(response.json()["peak_capital"], 5000.0)

    def test_invalid_risk_mode_returns_422(self):
        response = self.client.put("/api/settings/copy-trading", json={"risk_mode": "bogus"})
        self.assertEqual(response.status_code, 422)

    def test_invalid_mirror_percentage_returns_422(self):
        response = self.client.put("/api/settings/copy-trading", json={"mirror_percentage": 101})
        self.assertEqual(response.status_code, 422)

    def test_invalid_max_position_size_returns_422(self):
        response = self.client.put("/api/settings/copy-trading", json={"max_position_size": 0})
        self.assertEqual(response.status_code, 422)

    def test_invalid_kelly_fraction_returns_422(self):
        response = self.client.put("/api/settings/copy-trading", json={"kelly_fraction": 2.0})
        self.assertEqual(response.status_code, 422)

    def test_invalid_inverse_cooldown_returns_422(self):
        response = self.client.put(
            "/api/settings/copy-trading", json={"inverse_bot_cooldown_minutes": 1441}
        )
        self.assertEqual(response.status_code, 422)

    def test_invalid_inverse_confidence_returns_422(self):
        response = self.client.put(
            "/api/settings/copy-trading", json={"inverse_bot_confidence_threshold": 101}
        )
        self.assertEqual(response.status_code, 422)

    def test_invalid_inverse_size_mode_returns_422(self):
        response = self.client.put(
            "/api/settings/copy-trading",
            json={"inverse_bot_default_size_mode": "bogus"},
        )
        self.assertEqual(response.status_code, 422)

    def test_invalid_paper_balance_returns_422(self):
        response = self.client.put("/api/settings/copy-trading", json={"paper_balance": -1})
        self.assertEqual(response.status_code, 422)

    def test_invalid_monthly_loss_limit_returns_422(self):
        response = self.client.put("/api/settings/copy-trading", json={"monthly_loss_limit": -1})
        self.assertEqual(response.status_code, 422)


# ── GET /api/settings/paper-summary ─────────────────────


class PaperSummaryTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/paper-summary")
        self.assertEqual(response.status_code, 401)

    def test_returns_service_summary(self):
        service_summary = {
            "simulation_mode": True,
            "paper_balance": 500.0,
            "paper_pnl": 42.5,
            "simulated_trades": 3,
        }
        with patch(
            "app.services.simulation.get_paper_summary",
            return_value=service_summary,
        ) as mock_summary:
            response = self.client.get("/api/settings/paper-summary")
        self.assertEqual(response.status_code, 200)
        mock_summary.assert_called_once_with(self.db, 1)
        data = response.json()
        self.assertTrue(data["simulation_mode"])
        self.assertEqual(data["paper_balance"], 500.0)
        self.assertEqual(data["paper_pnl"], 42.5)
        self.assertEqual(data["simulated_trades"], 3)


# ── GET /api/settings/backends/status ───────────────────


class BackendsStatusTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/backends/status")
        self.assertEqual(response.status_code, 401)

    def test_both_backends_online(self):
        with self._patch_analysis_clients(True, True):
            response = self.client.get("/api/settings/backends/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["llm_chain"]["healthy"])
        self.assertEqual(data["llm_chain"]["status"], "online")
        self.assertEqual(data["llm_chain"]["name"], "LLM Chain (OpenAI)")
        self.assertTrue(data["cli_agent"]["healthy"])
        self.assertEqual(data["cli_agent"]["status"], "online")
        self.assertEqual(data["cli_agent"]["name"], "CLI Agent (GitHub Copilot)")

    def test_both_backends_offline(self):
        with self._patch_analysis_clients(False, False):
            response = self.client.get("/api/settings/backends/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["llm_chain"]["healthy"])
        self.assertEqual(data["llm_chain"]["status"], "offline")
        self.assertFalse(data["cli_agent"]["healthy"])
        self.assertEqual(data["cli_agent"]["status"], "offline")

    def test_mixed_backend_health(self):
        with self._patch_analysis_clients(True, False):
            response = self.client.get("/api/settings/backends/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["llm_chain"]["status"], "online")
        self.assertEqual(data["cli_agent"]["status"], "offline")


# ── Profile endpoints ───────────────────────────────────


class ProfileTests(SettingsRouteTestCase):
    def test_get_profile_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/profile")
        self.assertEqual(response.status_code, 401)

    def test_get_profile_user_not_found(self):
        response = self.client.get("/api/settings/profile")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "User not found")

    def test_get_profile_with_display_name(self):
        self.db.first_results[("User",)] = make_user(
            display_name="Trader",
            email="trader@example.com",
            phone="+1234567890",
            profile_picture_url="https://example.com/avatar.png",
        )
        response = self.client.get("/api/settings/profile")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertEqual(data["display_name"], "Trader")
        self.assertEqual(data["email"], "trader@example.com")
        self.assertEqual(data["phone"], "+1234567890")
        self.assertEqual(data["profile_picture_url"], "https://example.com/avatar.png")

    def test_get_profile_display_name_falls_back_to_wallet(self):
        self.db.first_results[("User",)] = make_user(display_name=None)
        response = self.client.get("/api/settings/profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_name"], WALLET)

    def test_update_profile_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.put("/api/settings/profile", json={})
        self.assertEqual(response.status_code, 401)

    def test_update_profile_user_not_found(self):
        response = self.client.put("/api/settings/profile", json={"display_name": "Trader"})
        self.assertEqual(response.status_code, 404)

    def test_update_profile_updates_fields(self):
        user = make_user()
        self.db.first_results[("User",)] = user
        response = self.client.put(
            "/api/settings/profile",
            json={
                "display_name": "Trader",
                "email": "trader@example.com",
                "phone": "+1234567890",
                "profile_picture_url": "https://example.com/avatar.png",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(user.display_name, "Trader")
        self.assertEqual(user.email, "trader@example.com")
        self.assertEqual(user.phone, "+1234567890")
        self.assertEqual(user.profile_picture_url, "https://example.com/avatar.png")
        self.assertTrue(self.db.committed)
        self.assertIn(user, self.db.refreshed)
        data = response.json()
        self.assertEqual(data["display_name"], "Trader")
        self.assertEqual(data["email"], "trader@example.com")

    def test_update_profile_empty_strings_clear_to_null(self):
        user = make_user(display_name="Trader", email="t@example.com", phone="+1")
        self.db.first_results[("User",)] = user
        response = self.client.put(
            "/api/settings/profile",
            json={"display_name": "", "email": "", "phone": "", "profile_picture_url": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(user.display_name)
        self.assertIsNone(user.email)
        self.assertIsNone(user.phone)
        self.assertIsNone(user.profile_picture_url)
        data = response.json()
        self.assertEqual(data["display_name"], WALLET)
        self.assertIsNone(data["email"])

    def test_update_profile_valid_data_url(self):
        user = make_user()
        self.db.first_results[("User",)] = user
        data_url = "data:image/png;base64,abc"
        response = self.client.put("/api/settings/profile", json={"profile_picture_url": data_url})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(user.profile_picture_url, data_url)

    def test_update_profile_rejects_non_http_scheme(self):
        response = self.client.put(
            "/api/settings/profile",
            json={"profile_picture_url": "ftp://example.com/a.png"},
        )
        self.assertEqual(response.status_code, 422)

    def test_update_profile_rejects_http_without_netloc(self):
        response = self.client.put("/api/settings/profile", json={"profile_picture_url": "http://"})
        self.assertEqual(response.status_code, 422)

    def test_update_profile_rejects_non_image_data_url(self):
        response = self.client.put(
            "/api/settings/profile",
            json={"profile_picture_url": "data:text/plain,abc"},
        )
        self.assertEqual(response.status_code, 422)

    def test_update_profile_display_name_too_long(self):
        response = self.client.put("/api/settings/profile", json={"display_name": "x" * 101})
        self.assertEqual(response.status_code, 422)

    def test_update_profile_email_too_long(self):
        response = self.client.put("/api/settings/profile", json={"email": "x" * 256})
        self.assertEqual(response.status_code, 422)

    def test_update_profile_phone_too_long(self):
        response = self.client.put("/api/settings/profile", json={"phone": "x" * 31})
        self.assertEqual(response.status_code, 422)


# ── LLM provider endpoints ──────────────────────────────


class LLMProvidersTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/llm/providers")
        self.assertEqual(response.status_code, 401)

    def test_lists_gateway_providers(self):
        providers_list = [
            {
                "id": "openai",
                "name": "OpenAI",
                "backend": "llm_chain",
                "models": ["gpt-4o"],
            },
            {
                "id": "ollama",
                "name": "Ollama",
                "backend": "llm_chain",
                "models": ["llama3"],
                "description": "Local Ollama models",
                "requires_api_key": False,
            },
        ]
        with self._patch_gateway(providers_list):
            response = self.client.get("/api/settings/llm/providers")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["default_provider"], "openai")
        self.assertEqual(len(data["providers"]), 2)
        first = data["providers"][0]
        self.assertEqual(first["id"], "openai")
        self.assertEqual(first["name"], "OpenAI")
        self.assertEqual(first["backend"], "llm_chain")
        self.assertEqual(first["models"], ["gpt-4o"])
        self.assertEqual(first["description"], "")
        self.assertTrue(first["requires_api_key"])
        second = data["providers"][1]
        self.assertEqual(second["description"], "Local Ollama models")
        self.assertFalse(second["requires_api_key"])


class LLMCurrentSettingsTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/llm/current")
        self.assertEqual(response.status_code, 401)

    def test_no_settings_defaults_to_openai(self):
        with self._patch_gateway([]):
            response = self.client.get("/api/settings/llm/current")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["provider"])
        self.assertIsNone(data["model"])
        self.assertEqual(data["effective_provider"], "openai")
        self.assertIn("gpt-4o", data["available_models"])

    def test_preferred_provider_is_effective(self):
        self.db.first_results[("UserSettings",)] = make_settings(
            preferred_llm_provider="anthropic",
            preferred_llm_model="claude-sonnet-4",
        )
        with self._patch_gateway([]):
            response = self.client.get("/api/settings/llm/current")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["provider"], "anthropic")
        self.assertEqual(data["model"], "claude-sonnet-4")
        self.assertEqual(data["effective_provider"], "anthropic")
        self.assertIn("claude-sonnet-4", data["available_models"])

    def test_cli_agent_backend_maps_to_github_models(self):
        self.db.first_results[("UserSettings",)] = make_settings(ai_backend="cli_agent")
        with self._patch_gateway([]):
            response = self.client.get("/api/settings/llm/current")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["effective_provider"], "github_models")
        self.assertIn("gpt-4o", data["available_models"])

    def test_unknown_provider_yields_no_models(self):
        self.db.first_results[("UserSettings",)] = make_settings(
            preferred_llm_provider="bogus_provider"
        )
        with self._patch_gateway([]):
            response = self.client.get("/api/settings/llm/current")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["provider"], "bogus_provider")
        self.assertEqual(data["effective_provider"], "bogus_provider")
        self.assertEqual(data["available_models"], [])


class UpdateLLMSettingsTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.patch("/api/settings/llm", json={})
        self.assertEqual(response.status_code, 401)

    def test_invalid_provider_returns_400(self):
        response = self.client.patch("/api/settings/llm", json={"provider": "bogus"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid provider", response.json()["detail"])

    def test_creates_new_settings_with_provider(self):
        response = self.client.patch(
            "/api/settings/llm",
            json={"provider": "anthropic", "model": "claude-sonnet-4"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.db.added), 1)
        created = self.db.added[0]
        self.assertEqual(created.user_id, 1)
        self.assertEqual(created.ai_backend, "llm_chain")
        self.assertEqual(created.preferred_llm_provider, "anthropic")
        self.assertEqual(created.preferred_llm_model, "claude-sonnet-4")
        self.assertTrue(self.db.committed)
        data = response.json()
        self.assertEqual(data["provider"], "anthropic")
        self.assertEqual(data["model"], "claude-sonnet-4")
        self.assertEqual(data["effective_provider"], "anthropic")

    def test_creates_new_settings_without_provider(self):
        response = self.client.patch("/api/settings/llm", json={"model": "gpt-4o"})
        self.assertEqual(response.status_code, 200)
        created = self.db.added[0]
        self.assertIsNone(created.preferred_llm_provider)
        self.assertEqual(created.preferred_llm_model, "gpt-4o")
        data = response.json()
        self.assertIsNone(data["provider"])
        self.assertEqual(data["effective_provider"], "openai")

    def test_updates_existing_settings(self):
        existing = make_settings(preferred_llm_provider="openai", preferred_llm_model="gpt-4o")
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch(
            "/api/settings/llm", json={"provider": "google", "model": "gemini-2.5-flash"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.preferred_llm_provider, "google")
        self.assertEqual(existing.preferred_llm_model, "gemini-2.5-flash")
        self.assertEqual(existing.ai_backend, "llm_chain")
        self.assertIsNotNone(existing.updated_at)
        data = response.json()
        self.assertEqual(data["provider"], "google")
        self.assertEqual(data["effective_provider"], "google")

    def test_provider_uppercase_is_normalized(self):
        existing = make_settings()
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/llm", json={"provider": "OpenAI"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.preferred_llm_provider, "openai")
        self.assertEqual(response.json()["effective_provider"], "openai")

    def test_clears_provider_and_model(self):
        existing = make_settings(
            preferred_llm_provider="anthropic", preferred_llm_model="claude-sonnet-4"
        )
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/llm", json={"provider": "", "model": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(existing.preferred_llm_provider)
        self.assertIsNone(existing.preferred_llm_model)
        data = response.json()
        self.assertIsNone(data["provider"])
        self.assertIsNone(data["model"])
        self.assertEqual(data["effective_provider"], "openai")

    def test_model_only_update_keeps_provider(self):
        existing = make_settings(preferred_llm_provider="openai")
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/llm", json={"model": "gpt-4o"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.preferred_llm_provider, "openai")
        self.assertEqual(existing.preferred_llm_model, "gpt-4o")
        self.assertEqual(response.json()["effective_provider"], "openai")

    def test_github_models_provider_sets_cli_agent_backend(self):
        existing = make_settings(ai_backend="llm_chain")
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/llm", json={"provider": "github_models"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.ai_backend, "cli_agent")
        self.assertEqual(existing.preferred_llm_provider, "github_models")
        data = response.json()
        self.assertEqual(data["effective_provider"], "github_models")
        self.assertIn("gpt-4o", data["available_models"])

    def test_empty_body_on_existing_settings(self):
        existing = make_settings(preferred_llm_provider="openai", preferred_llm_model="gpt-4o")
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/llm", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.preferred_llm_provider, "openai")
        self.assertEqual(existing.preferred_llm_model, "gpt-4o")
        self.assertIsNotNone(existing.updated_at)
        self.assertEqual(response.json()["effective_provider"], "openai")

    def test_cli_agent_backend_effective_github_models(self):
        existing = make_settings(ai_backend="cli_agent")
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/llm", json={"model": "gpt-4o"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["provider"])
        self.assertEqual(response.json()["effective_provider"], "github_models")
        self.assertIn("gpt-4o", response.json()["available_models"])

    def test_existing_invalid_provider_yields_no_models(self):
        existing = make_settings(preferred_llm_provider="bogus_provider")
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/llm", json={"model": "gpt-4o"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["provider"], "bogus_provider")
        self.assertEqual(data["effective_provider"], "bogus_provider")
        self.assertEqual(data["available_models"], [])


# ── Admin: require_admin dependency ─────────────────────


class RequireAdminTests(SettingsRouteTestCase):
    def test_non_admin_is_forbidden(self):
        self._authenticate(user_id=1, wallet=WALLET, is_admin=False)
        self.db.first_results[("User",)] = make_user(is_admin=False)
        response = self.client.get("/api/settings/admin/providers")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Admin access required")

    def test_missing_user_is_forbidden(self):
        self._authenticate()
        response = self.client.get("/api/settings/admin/providers")
        self.assertEqual(response.status_code, 403)

    def test_missing_user_id_is_forbidden(self):
        self._authenticate(user_id=None)
        response = self.client.get("/api/settings/admin/providers")
        self.assertEqual(response.status_code, 403)


# ── GET /api/settings/admin/providers ───────────────────


class AdminProvidersTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/admin/providers")
        self.assertEqual(response.status_code, 401)

    def test_lists_providers_with_status(self):
        self._make_admin()
        providers_list = [
            {
                "id": "openai",
                "name": "OpenAI",
                "backend": "llm_chain",
                "models": ["gpt-4o"],
                "description": "OpenAI API",
                "requires_api_key": True,
            },
            {
                "id": "github_models",
                "name": "GitHub Models",
                "backend": "cli_agent",
                "models": ["gpt-4o"],
                "description": "GitHub Models API",
                "requires_api_key": True,
            },
            {
                "id": "ollama",
                "name": "Ollama",
                "backend": "llm_chain",
                "models": ["llama3"],
            },
        ]
        env = {
            "OPENAI_API_KEY": "sk-test",
            "GITHUB_TOKEN": "ghp-test",
            "LLM_PROVIDER": "anthropic",
            "LLM_MODEL": "claude-sonnet-4",
        }
        with (
            patch.dict(os.environ, env),
            self._patch_gateway(providers_list),
            self._patch_analysis_clients(True, False),
        ):
            response = self.client.get("/api/settings/admin/providers")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["default_provider"], "anthropic")
        self.assertEqual(data["default_model"], "claude-sonnet-4")
        self.assertEqual(len(data["providers"]), 3)
        openai = data["providers"][0]
        self.assertEqual(openai["id"], "openai")
        self.assertTrue(openai["is_configured"])
        self.assertTrue(openai["is_healthy"])
        github = data["providers"][1]
        self.assertTrue(github["is_configured"])
        self.assertFalse(github["is_healthy"])
        ollama = data["providers"][2]
        # Ollama needs no API key, so it is always "configured".
        self.assertTrue(ollama["is_configured"])
        self.assertTrue(ollama["is_healthy"])
        self.assertEqual(ollama["description"], "")
        self.assertTrue(ollama["requires_api_key"])

    def test_health_check_exceptions_are_suppressed(self):
        self._make_admin()
        providers_list = [
            {
                "id": "openai",
                "name": "OpenAI",
                "backend": "llm_chain",
                "models": ["gpt-4o"],
            }
        ]
        with (
            self._patch_gateway(providers_list),
            self._patch_analysis_clients(RuntimeError("boom"), RuntimeError("boom")),
        ):
            response = self.client.get("/api/settings/admin/providers")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["providers"][0]["is_healthy"])


# ── PATCH /api/settings/admin/defaults ──────────────────


class AdminDefaultsTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.patch("/api/settings/admin/defaults", json={})
        self.assertEqual(response.status_code, 401)

    def test_non_admin_is_forbidden(self):
        self._authenticate()
        self.db.first_results[("User",)] = make_user(is_admin=False)
        response = self.client.patch(
            "/api/settings/admin/defaults", json={"default_provider": "openai"}
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_provider_returns_400(self):
        self._make_admin()
        response = self.client.patch(
            "/api/settings/admin/defaults", json={"default_provider": "bogus"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid provider", response.json()["detail"])

    def test_updates_provider_and_model(self):
        self._make_admin()
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("LLM_PROVIDER", None)
            env.pop("LLM_MODEL", None)
            response = self.client.patch(
                "/api/settings/admin/defaults",
                json={"default_provider": "Anthropic", "default_model": "claude-sonnet-4"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["default_provider"], "anthropic")
        self.assertEqual(data["default_model"], "claude-sonnet-4")
        self.assertIn("note", data)

    def test_model_only_update(self):
        self._make_admin()
        with patch.dict(os.environ, {"LLM_PROVIDER": "openai"}, clear=False):
            response = self.client.patch(
                "/api/settings/admin/defaults", json={"default_model": "gpt-4o"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["default_provider"], "openai")
        self.assertEqual(data["default_model"], "gpt-4o")

    def test_empty_body_returns_current_defaults(self):
        self._make_admin()
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("LLM_PROVIDER", None)
            env.pop("LLM_MODEL", None)
            response = self.client.patch("/api/settings/admin/defaults", json={})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["default_provider"], "openai")
        self.assertEqual(data["default_model"], "gpt-4o-mini")


# ── GET /api/settings/admin/users ───────────────────────


class AdminUsersTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.get("/api/settings/admin/users")
        self.assertEqual(response.status_code, 401)

    def test_non_admin_is_forbidden(self):
        self._authenticate()
        self.db.first_results[("User",)] = make_user(is_admin=False)
        response = self.client.get("/api/settings/admin/users")
        self.assertEqual(response.status_code, 403)

    def test_lists_users_with_settings(self):
        self._make_admin()
        user = make_user(id=1, wallet_address=WALLET, display_name="Trader")
        settings = make_settings(
            user_id=1,
            preferred_llm_provider="anthropic",
            preferred_llm_model="claude-sonnet-4",
            ai_backend="llm_chain",
            updated_at=NOW,
        )
        self.db.scalar_result = 1
        self.db.all_results[("User", "UserSettings")] = [(user, settings)]
        response = self.client.get("/api/settings/admin/users?skip=0&limit=10")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["users"]), 1)
        entry = data["users"][0]
        self.assertEqual(entry["user_id"], 1)
        self.assertEqual(entry["wallet_address"], WALLET)
        self.assertEqual(entry["display_name"], "Trader")
        self.assertEqual(entry["preferred_llm_provider"], "anthropic")
        self.assertEqual(entry["preferred_llm_model"], "claude-sonnet-4")
        self.assertEqual(entry["ai_backend"], "llm_chain")
        self.assertEqual(entry["updated_at"], NOW.isoformat())

    def test_user_without_settings_gets_defaults(self):
        self._make_admin()
        user = make_user(id=2, wallet_address=ADMIN_WALLET)
        self.db.scalar_result = 1
        self.db.all_results[("User", "UserSettings")] = [(user, None)]
        response = self.client.get("/api/settings/admin/users")
        self.assertEqual(response.status_code, 200)
        entry = response.json()["users"][0]
        self.assertIsNone(entry["preferred_llm_provider"])
        self.assertIsNone(entry["preferred_llm_model"])
        self.assertEqual(entry["ai_backend"], "llm_chain")
        self.assertIsNone(entry["updated_at"])

    def test_settings_without_updated_at(self):
        self._make_admin()
        user = make_user(id=1, wallet_address=WALLET)
        settings = make_settings(user_id=1, updated_at=None)
        self.db.scalar_result = 1
        self.db.all_results[("User", "UserSettings")] = [(user, settings)]
        response = self.client.get("/api/settings/admin/users")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["users"][0]["updated_at"])


# ── PATCH /api/settings/admin/users/{user_id}/llm ───────


class AdminUserLLMTests(SettingsRouteTestCase):
    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        response = self.client.patch(
            "/api/settings/admin/users/1/llm", json={"preferred_llm_provider": "openai"}
        )
        self.assertEqual(response.status_code, 401)

    def test_non_admin_is_forbidden(self):
        self._authenticate()
        self.db.first_results[("User",)] = make_user(is_admin=False)
        response = self.client.patch(
            "/api/settings/admin/users/1/llm", json={"preferred_llm_provider": "openai"}
        )
        self.assertEqual(response.status_code, 403)

    def test_user_not_found_returns_404(self):
        self._make_admin()
        # require_admin resolves the admin (token user_id 1) while
        # the route's own lookup for user 99 finds nothing.
        self.db.filter_results[(("User",), (99,))] = None
        response = self.client.patch(
            "/api/settings/admin/users/99/llm", json={"preferred_llm_provider": "openai"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "User 99 not found")

    def test_invalid_provider_returns_400(self):
        self._make_admin()
        response = self.client.patch(
            "/api/settings/admin/users/1/llm",
            json={"preferred_llm_provider": "bogus"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid provider", response.json()["detail"])

    def test_creates_settings_for_user(self):
        self._make_admin()
        response = self.client.patch(
            "/api/settings/admin/users/1/llm",
            json={"preferred_llm_provider": "anthropic", "preferred_llm_model": "claude-sonnet-4"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.db.added), 1)
        created = self.db.added[0]
        self.assertEqual(created.user_id, 1)
        self.assertEqual(created.preferred_llm_provider, "anthropic")
        self.assertEqual(created.preferred_llm_model, "claude-sonnet-4")
        self.assertEqual(created.ai_backend, "llm_chain")
        self.assertTrue(self.db.committed)
        data = response.json()
        self.assertEqual(data["user_id"], 1)
        self.assertEqual(data["preferred_llm_provider"], "anthropic")
        self.assertEqual(data["preferred_llm_model"], "claude-sonnet-4")
        self.assertEqual(data["ai_backend"], "llm_chain")
        # The fake session does not apply column defaults, so a
        # freshly-created record carries no updated_at yet.
        self.assertIsNone(data["updated_at"])

    def test_creates_settings_without_provider(self):
        self._make_admin()
        response = self.client.patch(
            "/api/settings/admin/users/1/llm", json={"preferred_llm_model": "gpt-4o"}
        )
        self.assertEqual(response.status_code, 200)
        created = self.db.added[0]
        self.assertIsNone(created.preferred_llm_provider)
        self.assertEqual(created.preferred_llm_model, "gpt-4o")
        self.assertEqual(response.json()["preferred_llm_provider"], None)

    def test_updates_existing_settings(self):
        self._make_admin()
        existing = make_settings(
            user_id=1, preferred_llm_provider="openai", preferred_llm_model="gpt-4o"
        )
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch(
            "/api/settings/admin/users/1/llm",
            json={"preferred_llm_provider": "google", "preferred_llm_model": "gemini-2.5-flash"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.preferred_llm_provider, "google")
        self.assertEqual(existing.preferred_llm_model, "gemini-2.5-flash")
        self.assertEqual(existing.ai_backend, "llm_chain")
        self.assertIsNotNone(existing.updated_at)
        data = response.json()
        self.assertEqual(data["preferred_llm_provider"], "google")
        self.assertEqual(data["preferred_llm_model"], "gemini-2.5-flash")

    def test_clears_provider(self):
        self._make_admin()
        existing = make_settings(
            user_id=1, preferred_llm_provider="anthropic", preferred_llm_model="claude-sonnet-4"
        )
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch(
            "/api/settings/admin/users/1/llm",
            json={"preferred_llm_provider": "", "preferred_llm_model": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(existing.preferred_llm_provider)
        self.assertIsNone(existing.preferred_llm_model)
        data = response.json()
        self.assertIsNone(data["preferred_llm_provider"])
        self.assertIsNone(data["preferred_llm_model"])

    def test_github_models_provider_sets_cli_agent_backend(self):
        self._make_admin()
        existing = make_settings(user_id=1, ai_backend="llm_chain")
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch(
            "/api/settings/admin/users/1/llm",
            json={"preferred_llm_provider": "github_models"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.ai_backend, "cli_agent")
        self.assertEqual(response.json()["ai_backend"], "cli_agent")

    def test_empty_body_updates_timestamp_only(self):
        self._make_admin()
        existing = make_settings(
            user_id=1, preferred_llm_provider="openai", preferred_llm_model="gpt-4o"
        )
        self.db.first_results[("UserSettings",)] = existing
        response = self.client.patch("/api/settings/admin/users/1/llm", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(existing.preferred_llm_provider, "openai")
        self.assertEqual(existing.preferred_llm_model, "gpt-4o")
        self.assertIsNotNone(existing.updated_at)
        self.assertEqual(response.json()["preferred_llm_provider"], "openai")


if __name__ == "__main__":
    unittest.main()
