"""Coverage tests for the /api/trades routes (``app.api.routes.trading.trades``).

Every route and every branch is exercised against an in-memory fake
DB session (``FakeSession``/``FakeQuery``) with all external I/O
patched: the credential store, the pre-trade safety gate, order
placement on Polymarket, the leaderboard/trader-profile scrapers,
the copy-trade service, the kelly sizing service, the trader-quality
service, the arbitrage service and the execution-analytics service.

Covered:

* ``_safe_json_obj`` -- None / invalid / non-dict / valid-dict inputs.
* ``GET /history`` -- credential loading (including CredentialStoreError),
  has_more boundary, empty history.
* ``POST /execute`` -- 503 on credential-store failure, 400 without
  credentials, idempotent replay, settings auto-creation, the
  max_position_size cap, safety-cap rejection, order success/failure,
  the DB-record failure rollback path and idempotent result storage.
* ``POST /cash-out`` -- forces SELL regardless of the request body.
* ``GET /leaderboard`` -- anonymous and authenticated (follow /
  notification-follow flags).
* ``GET /trader/{wallet}`` -- 404, real win-rate override, markets
  max(), trade_stats suppression.
* ``POST/DELETE /follow/{wallet}`` -- new follow, reactivation with
  per-field updates, alias normalisation, 404, monitor removal only
  when no watchers remain.
* ``GET /following``.
* ``POST/DELETE /notification-follow/{wallet}`` -- new, update, the
  both-disabled fallback, 404, monitor removal.
* ``GET /notification-following``.
* ``GET /following-feed`` -- wallet / event_type filters and null-field
  defaults.
* ``GET /copy-trades`` -- full and minimal rows, calculation_details
  JSON parsing.
* ``GET /copy-evaluation/{wallet}`` and ``GET /copy-trades/pnl``.
* ``GET /copy-trades/pnl/by-trader`` -- window default, forwarded window,
  out-of-range 422, auth, and the unchanged daily-PnL response shape.
* ``POST/GET/DELETE /stop-loss`` and ``/take-profit`` -- create, update,
  list (status filter + all), cancel (404 / non-active / success).
* ``POST /emergency-stop`` -- durable operation acknowledgment without signing.
* ``POST /resume-trading`` -- verified recovery required (409).
* ``GET /trader/{wallet}/quality`` and ``POST /traders/rescore``.
* ``GET /arbitrage/opportunities`` and ``POST /arbitrage/scan``.
* ``GET /analytics/summary|edge-score|trades`` -- including the
  unknown-strategy 400.
* ``GET /size-suggestion`` -- no-edge, price fetch success/failure,
  price out of range, every capped_by branch and the safety-cap
  rejection branch.
"""

import contextlib
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.api.routes.auth.auth import (
    get_current_user_from_token,
    get_optional_user_from_token,
)
from app.api.routes.trading.trades import _safe_json_obj
from app.main import app
from app.models.notifications.notification_feed_event import NotificationFeedEvent
from app.models.notifications.notification_followed_trader import NotificationFollowedTrader
from app.models.trading.followed_trader import FollowedTrader
from app.models.trading.stop_loss import StopLossOrder
from app.models.trading.take_profit import TakeProfitOrder
from app.models.users.user_settings import UserSettings
from app.security.credential_store import CredentialStoreError
from app.services.trading import kelly_service
from app.utils.database import get_db
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20
TRADER = "0x" + "cd" * 20
PRIVATE_KEY = "0x" + "11" * 32
CLOB_CREDS = {"api_key": "k", "api_secret": "s", "api_passphrase": "p"}


# ────────────── Fake DB ──────────────


class FakeQuery:
    """Stand-in for a SQLAlchemy query: filter/order/limit are no-ops."""

    def __init__(self, first=None, all_results=None, update_count=0, first_results=None):
        self._first = first
        self._first_results = list(first_results) if first_results else None
        self._all = list(all_results or [])
        self._update_count = update_count

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def first(self):
        if self._first_results:
            return self._first_results.pop(0)
        return self._first

    def all(self):
        return list(self._all)

    def update(self, values):
        return self._update_count


class FakeSession:
    """Stand-in for a SQLAlchemy session with column-default semantics."""

    def __init__(self):
        self._registry = {}
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshed = []
        self.commit_error = None
        self._next_id = 1

    def register(self, model, first=None, all_results=None, update_count=0, first_results=None):
        query = FakeQuery(
            first=first,
            all_results=all_results,
            update_count=update_count,
            first_results=first_results,
        )
        self._registry[model] = query
        return query

    def query(self, *models):
        return self._registry.setdefault(models[0], FakeQuery())

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1
        # Mimic SQLAlchemy column defaults / primary-key assignment.
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = utc_now()
            if getattr(obj, "updated_at", None) is None:
                obj.updated_at = utc_now()

    def rollback(self):
        self.rollbacks += 1

    def refresh(self, obj):
        self.refreshed.append(obj)


def _authenticate(user_id=1, wallet=WALLET):
    app.dependency_overrides[get_current_user_from_token] = lambda: {
        "user_id": user_id,
        "wallet_address": wallet,
        "is_admin": False,
    }


class _TradesRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        _authenticate()
        self.addCleanup(app.dependency_overrides.clear)


@contextlib.contextmanager
def _execute_env(
    stored=None,
    place_result=None,
    caps=None,
    prior=None,
    commit_error=None,
    settings_first=None,
    creds_error=None,
):
    """Patch everything ``execute_trade`` touches and yield the doubles."""
    session = FakeSession()
    if settings_first is not None:
        session.register(UserSettings, first=settings_first)
    session.commit_error = commit_error
    place = MagicMock(
        return_value=place_result
        if place_result is not None
        else {"success": True, "order_hash": "0xorderhash"}
    )
    caps = MagicMock(return_value=caps if caps is not None else (5.0, None))
    idem_get = MagicMock(return_value=prior)
    idem_store = MagicMock()
    if creds_error is not None:
        creds = MagicMock(side_effect=creds_error)
    else:
        creds = MagicMock(return_value=stored)
    app.dependency_overrides[get_db] = lambda: session
    try:
        with (
            # Legacy handler branches use fake DB/SDK I/O; security tests
            # separately assert the production release barrier.
            patch("app.services.trading.live_trading_policy.block_legacy_live_execution"),
            patch("app.api.routes.trading.trades.load_wallet_credentials", creds),
            patch("app.api.routes.trading.trades.get_idempotent_order_result", idem_get),
            patch("app.api.routes.trading.trades.store_idempotent_order_result", idem_store),
            patch("app.api.routes.trading.trades.apply_global_safety_caps", caps),
            patch("app.api.routes.trading.trades._place_order_on_polymarket", place),
        ):
            yield session, place, caps, idem_get, idem_store
    finally:
        app.dependency_overrides.pop(get_db, None)


EXECUTE_BODY = {
    "execution_mode": "live",
    "token_id": "tok",
    "market_id": "mkt",
    "side": "BUY",
    "price": 0.5,
    "size": 10.0,
}


# ────────────── _safe_json_obj ──────────────


class SafeJsonObjTests(unittest.TestCase):
    def test_none_and_empty_return_none(self):
        self.assertIsNone(_safe_json_obj(None))
        self.assertIsNone(_safe_json_obj(""))

    def test_invalid_json_returns_none(self):
        self.assertIsNone(_safe_json_obj("{not json"))

    def test_non_dict_json_returns_none(self):
        self.assertIsNone(_safe_json_obj("[1, 2, 3]"))
        self.assertIsNone(_safe_json_obj("42"))

    def test_valid_dict_is_returned(self):
        self.assertEqual(_safe_json_obj('{"a": 1}'), {"a": 1})


# ────────────── Trade history ──────────────


class TradeHistoryTests(_TradesRouteTestCase):
    def test_history_returns_trades(self):
        service = MagicMock()
        service.get_trade_history = AsyncMock(
            return_value=[
                {"id": "t1", "market": "m", "side": "BUY", "size": 1.0, "price": 0.5},
            ]
        )
        stored = {"private_key": PRIVATE_KEY, "clob_creds": CLOB_CREDS}
        with (
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=service),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=stored),
        ):
            resp = self.client.get("/api/trades/history?limit=10&offset=0")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertEqual(data["count"], 1)
        self.assertFalse(data["has_more"])
        service.get_trade_history.assert_awaited_once_with(
            WALLET,
            private_key=PRIVATE_KEY,
            clob_creds=CLOB_CREDS,
            limit=10,
            offset=0,
        )

    def test_history_has_more_when_count_equals_limit(self):
        service = MagicMock()
        service.get_trade_history = AsyncMock(return_value=[{"id": "t1", "side": "BUY"}])
        with (
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=service),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=None),
        ):
            resp = self.client.get("/api/trades/history?limit=1")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["has_more"])
        service.get_trade_history.assert_awaited_once_with(
            WALLET, private_key=None, clob_creds=None, limit=1, offset=0
        )

    def test_history_empty(self):
        service = MagicMock()
        service.get_trade_history = AsyncMock(return_value=[])
        with (
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=service),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=None),
        ):
            resp = self.client.get("/api/trades/history")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 0)
        self.assertEqual(resp.json()["trades"], [])

    def test_history_credential_store_error_uses_no_credentials(self):
        service = MagicMock()
        service.get_trade_history = AsyncMock(return_value=[])
        with (
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=service),
            patch(
                "app.api.routes.trading.trades.load_wallet_credentials",
                side_effect=CredentialStoreError("down"),
            ),
        ):
            resp = self.client.get("/api/trades/history")

        self.assertEqual(resp.status_code, 200)
        service.get_trade_history.assert_awaited_once_with(
            WALLET, private_key=None, clob_creds=None, limit=50, offset=0
        )


# ────────────── Execute ──────────────


# ────────────── Leaderboard ──────────────


class LeaderboardTests(_TradesRouteTestCase):
    ENTRY = {
        "address": TRADER,
        "display_name": "Trader",
        "profit_loss": 1.5,
        "volume": 10.0,
        "markets_traded": 3,
        "win_rate": 0.6,
        "positions_value": 5.0,
        "pnl_24h": 1.0,
        "pnl_7d": 2.0,
        "pnl_30d": 3.0,
        "volume_24h": 4.0,
        "profile_image": None,
        "quality_score": 80.0,
        "quality_tier": "A",
    }

    def test_leaderboard_anonymous(self):
        app.dependency_overrides[get_optional_user_from_token] = lambda: None
        with patch(
            "app.api.routes.trading.trades.fetch_leaderboard",
            AsyncMock(return_value=[self.ENTRY]),
        ):
            resp = self.client.get("/api/trades/leaderboard?limit=25&period=24h")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["period"], "24h")
        self.assertEqual(data["total"], 1)
        entry = data["entries"][0]
        self.assertEqual(entry["rank"], 1)
        self.assertEqual(entry["address"], TRADER)
        self.assertFalse(entry["is_followed"])
        self.assertFalse(entry["is_notification_followed"])
        self.assertEqual(entry["quality_score"], 80.0)

    def test_leaderboard_marks_followed_traders(self):
        app.dependency_overrides[get_optional_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        session = FakeSession()
        session.register(
            FollowedTrader.trader_wallet,
            all_results=[SimpleNamespace(trader_wallet=TRADER.upper())],
        )
        session.register(
            NotificationFollowedTrader.trader_wallet,
            all_results=[SimpleNamespace(trader_wallet=TRADER)],
        )
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.api.routes.trading.trades.fetch_leaderboard",
            AsyncMock(return_value=[self.ENTRY]),
        ):
            resp = self.client.get("/api/trades/leaderboard")

        self.assertEqual(resp.status_code, 200)
        entry = resp.json()["entries"][0]
        self.assertTrue(entry["is_followed"])
        self.assertTrue(entry["is_notification_followed"])

    def test_leaderboard_empty(self):
        app.dependency_overrides[get_optional_user_from_token] = lambda: None
        with patch(
            "app.api.routes.trading.trades.fetch_leaderboard",
            AsyncMock(return_value=[]),
        ):
            resp = self.client.get("/api/trades/leaderboard")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["entries"], [])
        self.assertEqual(resp.json()["total"], 0)


# ────────────── Trader profile ──────────────


class TraderProfileTests(_TradesRouteTestCase):
    def test_trader_profile_not_found(self):
        with patch(
            "app.api.routes.trading.trades.fetch_trader_profile",
            AsyncMock(return_value=None),
        ):
            resp = self.client.get(f"/api/trades/trader/{TRADER}")

        self.assertEqual(resp.status_code, 404)

    def test_trader_profile_uses_real_stats(self):
        profile = {
            "display_name": "Trader",
            "profit_loss": 10.0,
            "volume": 100.0,
            "markets_traded": 4,
            "win_rate": 0.4,
            "positions": [{"market": "m"}],
            "recent_trades": [{"id": "t"}],
            "profile_image": "img",
        }
        trade_data = {
            "stats": {
                "win_rate": 0.75,
                "unique_markets": 9,
                "total_trades": 50,
            }
        }
        with (
            patch(
                "app.api.routes.trading.trades.fetch_trader_profile",
                AsyncMock(return_value=profile),
            ),
            patch(
                "app.api.routes.trading.trades.fetch_trader_trades",
                AsyncMock(return_value=trade_data),
            ),
        ):
            resp = self.client.get(f"/api/trades/trader/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet_address"], TRADER)
        # Real win rate overrides the leaderboard value.
        self.assertEqual(data["win_rate"], 0.75)
        # markets_traded is the max of profile and real stats.
        self.assertEqual(data["markets_traded"], 9)
        self.assertEqual(data["trade_stats"]["total_trades"], 50)

    def test_trader_profile_falls_back_to_profile_win_rate(self):
        profile = {"display_name": "T", "markets_traded": 4, "win_rate": 0.4}
        trade_data = {"stats": {"win_rate": 0.0, "unique_markets": 2, "total_trades": 5}}
        with (
            patch(
                "app.api.routes.trading.trades.fetch_trader_profile",
                AsyncMock(return_value=profile),
            ),
            patch(
                "app.api.routes.trading.trades.fetch_trader_trades",
                AsyncMock(return_value=trade_data),
            ),
        ):
            resp = self.client.get(f"/api/trades/trader/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["win_rate"], 0.4)
        self.assertEqual(data["markets_traded"], 4)
        self.assertIsNotNone(data["trade_stats"])

    def test_trader_profile_hides_empty_stats(self):
        profile = {"display_name": "T", "markets_traded": 0, "win_rate": None}
        trade_data = {"stats": {"win_rate": None, "unique_markets": 0, "total_trades": 0}}
        with (
            patch(
                "app.api.routes.trading.trades.fetch_trader_profile",
                AsyncMock(return_value=profile),
            ),
            patch(
                "app.api.routes.trading.trades.fetch_trader_trades",
                AsyncMock(return_value=trade_data),
            ),
        ):
            resp = self.client.get(f"/api/trades/trader/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data["win_rate"])
        self.assertIsNone(data["trade_stats"])


# ────────────── Follow / unfollow ──────────────


def _followed_record(**overrides):
    base = {
        "id": 7,
        "user_id": 1,
        "trader_wallet": TRADER,
        "is_active": False,
        "max_position_size": None,
        "trader_alias": None,
        "sizing_mode": "inherit_global",
        "fixed_trade_amount_override": None,
        "copy_wallet_mode": "dynamic_main_wallet_percentage",
        "copy_wallet_percentage": 100.0,
        "copy_wallet_fixed_amount": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FollowTests(_TradesRouteTestCase):
    def test_follow_new_trader(self):
        session = FakeSession()
        session.register(FollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        add_watched = MagicMock()
        seed = AsyncMock()
        with (
            patch("app.services.trading.trade_monitor.add_watched_wallet", add_watched),
            patch("app.services.trading.trade_monitor.seed_wallet_cursor", seed),
        ):
            resp = self.client.post(f"/api/trades/follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["trader_wallet"], TRADER)
        self.assertTrue(data["is_active"])
        self.assertEqual(data["sizing_mode"], "inherit_global")
        self.assertEqual(data["copy_wallet_mode"], "dynamic_main_wallet_percentage")
        self.assertEqual(data["copy_wallet_percentage"], 100.0)
        self.assertIsNone(data["trader_alias"])
        add_watched.assert_called_once_with(TRADER)
        seed.assert_awaited_once()

    def test_follow_reactivates_and_updates_fields(self):
        record = _followed_record()
        session = FakeSession()
        session.register(FollowedTrader, first=record)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        body = {
            "max_position_size": 25.0,
            "trader_alias": "  Bob  ",
            "sizing_mode": "fixed_amount",
            "fixed_trade_amount_override": 10.0,
            "copy_wallet_mode": "fixed_snapshot_amount",
            "copy_wallet_percentage": 50.0,
            "copy_wallet_fixed_amount": 5.0,
        }
        with (
            patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()),
            patch("app.services.trading.trade_monitor.seed_wallet_cursor", AsyncMock()),
        ):
            resp = self.client.post(f"/api/trades/follow/{TRADER}", json=body)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], 7)
        self.assertTrue(data["is_active"])
        self.assertEqual(data["max_position_size"], 25.0)
        self.assertEqual(data["trader_alias"], "Bob")
        self.assertEqual(data["sizing_mode"], "fixed_amount")
        self.assertEqual(data["fixed_trade_amount_override"], 10.0)
        self.assertEqual(data["copy_wallet_mode"], "fixed_snapshot_amount")
        self.assertEqual(data["copy_wallet_percentage"], 50.0)
        self.assertEqual(data["copy_wallet_fixed_amount"], 5.0)

    def test_follow_normalises_blank_alias_to_none(self):
        session = FakeSession()
        session.register(FollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with (
            patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()),
            patch("app.services.trading.trade_monitor.seed_wallet_cursor", AsyncMock()),
        ):
            resp = self.client.post(
                f"/api/trades/follow/{TRADER}",
                json={"trader_alias": "   "},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["trader_alias"])

    def test_follow_existing_blank_alias_clears_alias(self):
        record = _followed_record(trader_alias="Old")
        session = FakeSession()
        session.register(FollowedTrader, first=record)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with (
            patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()),
            patch("app.services.trading.trade_monitor.seed_wallet_cursor", AsyncMock()),
        ):
            resp = self.client.post(
                f"/api/trades/follow/{TRADER}",
                json={"trader_alias": None},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["trader_alias"])

    def test_follow_monitor_failure_is_swallowed(self):
        session = FakeSession()
        session.register(FollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with (
            patch(
                "app.services.trading.trade_monitor.add_watched_wallet",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "app.services.trading.trade_monitor.seed_wallet_cursor",
                side_effect=RuntimeError("boom"),
            ),
        ):
            resp = self.client.post(f"/api/trades/follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)


class UnfollowTests(_TradesRouteTestCase):
    def test_unfollow_removes_from_monitor_when_no_watchers(self):
        record = _followed_record()
        session = FakeSession()
        session.register(FollowedTrader, first_results=[record, None])
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        remove_watched = MagicMock()
        with patch("app.services.trading.trade_monitor.remove_watched_wallet", remove_watched):
            resp = self.client.delete(f"/api/trades/follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "unfollowed", "wallet": TRADER})
        self.assertFalse(record.is_active)
        remove_watched.assert_called_once_with(TRADER)

    def test_unfollow_keeps_monitor_when_copy_watcher_remains(self):
        record = _followed_record()
        session = FakeSession()
        session.register(
            FollowedTrader,
            first_results=[record, SimpleNamespace(trader_wallet=TRADER)],
        )
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        remove_watched = MagicMock()
        with patch("app.services.trading.trade_monitor.remove_watched_wallet", remove_watched):
            resp = self.client.delete(f"/api/trades/follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        remove_watched.assert_not_called()

    def test_unfollow_not_following_returns_404(self):
        session = FakeSession()
        session.register(FollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete(f"/api/trades/follow/{TRADER}")

        self.assertEqual(resp.status_code, 404)

    def test_unfollow_monitor_failure_is_swallowed(self):
        record = _followed_record()
        session = FakeSession()
        session.register(FollowedTrader, first_results=[record, None])
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.trading.trade_monitor.remove_watched_wallet",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.delete(f"/api/trades/follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)


class GetFollowingTests(_TradesRouteTestCase):
    def test_get_following(self):
        session = FakeSession()
        session.register(
            FollowedTrader,
            all_results=[_followed_record(), _followed_record(id=8)],
        )
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/following")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["id"], 7)
        self.assertEqual(data[1]["id"], 8)

    def test_get_following_empty(self):
        session = FakeSession()
        session.register(FollowedTrader, all_results=[])
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/following")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


# ────────────── Notification follow ──────────────


def _notif_record(**overrides):
    base = {
        "id": 3,
        "user_id": 1,
        "trader_wallet": TRADER,
        "is_active": False,
        "feed_enabled": True,
        "email_enabled": False,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class NotificationFollowTests(_TradesRouteTestCase):
    def test_notification_follow_new(self):
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        add_watched = MagicMock()
        with patch("app.services.trading.trade_monitor.add_watched_wallet", add_watched):
            resp = self.client.post(f"/api/trades/notification-follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["trader_wallet"], TRADER)
        self.assertTrue(data["is_active"])
        self.assertTrue(data["feed_enabled"])
        self.assertFalse(data["email_enabled"])
        add_watched.assert_called_once_with(TRADER)

    def test_notification_follow_new_with_both_flags(self):
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()):
            resp = self.client.post(
                f"/api/trades/notification-follow/{TRADER}",
                json={"feed_enabled": False, "email_enabled": True},
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["feed_enabled"])
        self.assertTrue(data["email_enabled"])

    def test_notification_follow_new_both_disabled_forces_feed(self):
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()):
            resp = self.client.post(
                f"/api/trades/notification-follow/{TRADER}",
                json={"feed_enabled": False, "email_enabled": False},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["feed_enabled"])
        self.assertFalse(resp.json()["email_enabled"])

    def test_notification_follow_updates_existing(self):
        record = _notif_record(feed_enabled=False, email_enabled=False)
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=record)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()):
            resp = self.client.post(
                f"/api/trades/notification-follow/{TRADER}",
                json={"feed_enabled": True, "email_enabled": True},
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["is_active"])
        self.assertTrue(data["feed_enabled"])
        self.assertTrue(data["email_enabled"])

    def test_notification_follow_existing_both_disabled_forces_feed(self):
        record = _notif_record(feed_enabled=False, email_enabled=False)
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=record)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()):
            resp = self.client.post(
                f"/api/trades/notification-follow/{TRADER}",
                json={"feed_enabled": False, "email_enabled": False},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["feed_enabled"])
        self.assertFalse(resp.json()["email_enabled"])

    def test_notification_follow_existing_ignores_null_flags(self):
        record = _notif_record(feed_enabled=True, email_enabled=True)
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=record)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch("app.services.trading.trade_monitor.add_watched_wallet", MagicMock()):
            resp = self.client.post(
                f"/api/trades/notification-follow/{TRADER}",
                json={"feed_enabled": None, "email_enabled": None},
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["feed_enabled"])
        self.assertTrue(data["email_enabled"])

    def test_notification_follow_monitor_failure_is_swallowed(self):
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.trading.trade_monitor.add_watched_wallet",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.post(f"/api/trades/notification-follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)


class NotificationUnfollowTests(_TradesRouteTestCase):
    def test_notification_unfollow_removes_from_monitor(self):
        record = _notif_record()
        session = FakeSession()
        session.register(NotificationFollowedTrader, first_results=[record, None])
        session.register(FollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        remove_watched = MagicMock()
        with patch("app.services.trading.trade_monitor.remove_watched_wallet", remove_watched):
            resp = self.client.delete(f"/api/trades/notification-follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"status": "notification_unfollowed", "wallet": TRADER},
        )
        self.assertFalse(record.is_active)
        self.assertFalse(record.feed_enabled)
        self.assertFalse(record.email_enabled)
        remove_watched.assert_called_once_with(TRADER)

    def test_notification_unfollow_keeps_monitor_when_watcher_remains(self):
        record = _notif_record()
        session = FakeSession()
        session.register(NotificationFollowedTrader, first_results=[record, None])
        session.register(
            FollowedTrader,
            first=SimpleNamespace(trader_wallet=TRADER),
        )
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        remove_watched = MagicMock()
        with patch("app.services.trading.trade_monitor.remove_watched_wallet", remove_watched):
            resp = self.client.delete(f"/api/trades/notification-follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        remove_watched.assert_not_called()

    def test_notification_unfollow_monitor_failure_is_swallowed(self):
        record = _notif_record()
        session = FakeSession()
        session.register(NotificationFollowedTrader, first_results=[record, None])
        session.register(FollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.trading.trade_monitor.remove_watched_wallet",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.delete(f"/api/trades/notification-follow/{TRADER}")

        self.assertEqual(resp.status_code, 200)

    def test_notification_unfollow_not_following_returns_404(self):
        session = FakeSession()
        session.register(NotificationFollowedTrader, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete(f"/api/trades/notification-follow/{TRADER}")

        self.assertEqual(resp.status_code, 404)


class GetNotificationFollowingTests(_TradesRouteTestCase):
    def test_get_notification_following(self):
        session = FakeSession()
        session.register(
            NotificationFollowedTrader,
            all_results=[_notif_record(), _notif_record(id=4)],
        )
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/notification-following")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["id"], 3)
        self.assertEqual(data[1]["id"], 4)


class FollowingFeedTests(_TradesRouteTestCase):
    def _event(self, **overrides):
        base = {
            "id": 1,
            "trader_wallet": TRADER,
            "event_type": "opened",
            "market_id": "mkt",
            "token_id": "tok",
            "side": "BUY",
            "size": 1.0,
            "price": 0.5,
            "prev_net_size": 0.0,
            "new_net_size": 1.0,
            "source_trade_history_id": 9,
            "email_status": "sent",
            "email_error": None,
            "emailed_at": datetime(2026, 1, 2, tzinfo=UTC),
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_following_feed_no_filters(self):
        session = FakeSession()
        session.register(NotificationFeedEvent, all_results=[self._event()])
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/following-feed")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["event_type"], "opened")
        self.assertEqual(data[0]["email_status"], "sent")
        self.assertEqual(data[0]["emailed_at"], "2026-01-02T00:00:00+00:00")

    def test_following_feed_with_filters(self):
        session = FakeSession()
        session.register(NotificationFeedEvent, all_results=[])
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get(
            "/api/trades/following-feed",
            params={"wallet": TRADER, "event_type": "closed", "limit": 10},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_following_feed_null_fields_default(self):
        session = FakeSession()
        session.register(
            NotificationFeedEvent,
            all_results=[
                self._event(
                    market_id=None,
                    token_id=None,
                    side=None,
                    size=None,
                    price=None,
                    prev_net_size=None,
                    new_net_size=None,
                    email_status=None,
                    emailed_at=None,
                    created_at=None,
                )
            ],
        )
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/following-feed")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()[0]
        self.assertEqual(data["market_id"], "")
        self.assertEqual(data["token_id"], "")
        self.assertEqual(data["side"], "")
        self.assertEqual(data["size"], 0.0)
        self.assertEqual(data["price"], 0.0)
        self.assertEqual(data["prev_net_size"], 0.0)
        self.assertEqual(data["new_net_size"], 0.0)
        self.assertEqual(data["email_status"], "skipped")
        self.assertIsNone(data["emailed_at"])
        self.assertEqual(data["created_at"], "")


# ────────────── Copy-trade history ──────────────


class CopyTradesTests(_TradesRouteTestCase):
    def test_copy_trades_full_row(self):
        trades = [
            {
                "id": 1,
                "copied_from_wallet": TRADER,
                "market_id": "mkt",
                "action": "buy",
                "amount": 10.0,
                "price": 0.5,
                "status": "executed",
                "pnl": 1.5,
                "executed_at": "2026-01-01T00:00:00+00:00",
                "created_at": "2026-01-01T00:00:00+00:00",
                "source_trade_history_id": 9,
                "trader_trade_notional": 5.0,
                "trader_wallet_balance": 100.0,
                "copy_wallet_base": 50.0,
                "sizing_mode_applied": "fixed_amount",
                "copy_wallet_mode_applied": "fixed_snapshot_amount",
                "calculation_warning": "warn",
                "calculation_details": '{"ratio": 0.5}',
            }
        ]
        with patch(
            "app.api.routes.trading.trades.get_copy_trade_history",
            AsyncMock(return_value=trades),
        ):
            resp = self.client.get("/api/trades/copy-trades?limit=5")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        row = data[0]
        self.assertEqual(row["trader_wallet"], TRADER)
        self.assertEqual(row["pnl"], 1.5)
        self.assertEqual(row["calculation_details"], {"ratio": 0.5})
        self.assertEqual(row["sizing_mode_applied"], "fixed_amount")

    def test_copy_trades_minimal_row(self):
        with patch(
            "app.api.routes.trading.trades.get_copy_trade_history",
            AsyncMock(return_value=[{"id": 1}]),
        ):
            resp = self.client.get("/api/trades/copy-trades")

        self.assertEqual(resp.status_code, 200)
        row = resp.json()[0]
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["trader_wallet"], "")
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["pnl"])
        self.assertIsNone(row["calculation_details"])
        self.assertEqual(row["timestamp"], "")

    def test_copy_trades_bad_calculation_details_json(self):
        trades = [
            {"id": 1, "calculation_details": "{broken"},
            {"id": 2, "calculation_details": "[1, 2]"},
        ]
        with patch(
            "app.api.routes.trading.trades.get_copy_trade_history",
            AsyncMock(return_value=trades),
        ):
            resp = self.client.get("/api/trades/copy-trades")

        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertIsNone(rows[0]["calculation_details"])
        self.assertIsNone(rows[1]["calculation_details"])


class CopyEvaluationTests(_TradesRouteTestCase):
    def test_copy_evaluation(self):
        rows = [
            {
                "source_trade_id": 1,
                "market_id": "mkt",
                "side": "BUY",
                "price": 0.5,
                "source_trade_notional": 10.0,
                "copy_status": "copied",
            }
        ]
        with patch(
            "app.api.routes.trading.trades.get_copy_trade_evaluation",
            AsyncMock(return_value=rows),
        ):
            resp = self.client.get(f"/api/trades/copy-evaluation/{TRADER}")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet"], TRADER)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["rows"][0]["source_trade_id"], 1)
        self.assertEqual(data["rows"][0]["copy_status"], "copied")
        self.assertIn("updated_at", data)


class CopyPnlTests(_TradesRouteTestCase):
    def test_copy_trade_pnl(self):
        with patch(
            "app.api.routes.trading.trades.get_daily_copy_pnl",
            AsyncMock(return_value=123.45),
        ):
            resp = self.client.get("/api/trades/copy-trades/pnl")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"daily_pnl": 123.45})


# ────────────── Stop-loss ──────────────


def _sl_row(**overrides):
    base = {
        "id": 1,
        "token_id": "tok",
        "market_id": "mkt",
        "market_title": "Title",
        "outcome": "Yes",
        "size": 2.0,
        "stop_price": 0.4,
        "status": "active",
        "order_hash": None,
        "executed_price": None,
        "triggered_at": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class StopLossTests(_TradesRouteTestCase):
    SL_BODY = {
        "token_id": "tok",
        "market_id": "mkt",
        "market_title": "Title",
        "outcome": "Yes",
        "size": 2.0,
        "stop_price": 0.4,
    }

    def test_set_stop_loss_creates(self):
        session = FakeSession()
        session.register(StopLossOrder, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.post("/api/trades/stop-loss", json=self.SL_BODY)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["stop_price"], 0.4)
        self.assertEqual(data["size"], 2.0)
        self.assertIsInstance(session.added[0], StopLossOrder)

    def test_set_stop_loss_updates_existing(self):
        existing = _sl_row(stop_price=0.3, market_title="Old", outcome="No")
        session = FakeSession()
        session.register(StopLossOrder, first=existing)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.post("/api/trades/stop-loss", json=self.SL_BODY)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["stop_price"], 0.4)
        self.assertEqual(data["size"], 2.0)
        self.assertEqual(data["market_title"], "Title")
        self.assertEqual(data["outcome"], "Yes")

    def test_set_stop_loss_keeps_existing_title_and_outcome(self):
        existing = _sl_row(market_title="Keep", outcome="Maybe")
        session = FakeSession()
        session.register(StopLossOrder, first=existing)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        body = dict(self.SL_BODY, market_title="", outcome="")
        resp = self.client.post("/api/trades/stop-loss", json=body)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["market_title"], "Keep")
        self.assertEqual(resp.json()["outcome"], "Maybe")

    def test_get_stop_losses_filtered(self):
        session = FakeSession()
        session.register(StopLossOrder, all_results=[_sl_row()])
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/stop-loss?status=active")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_get_stop_losses_all(self):
        session = FakeSession()
        session.register(StopLossOrder, all_results=[_sl_row(), _sl_row(id=2)])
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/stop-loss?status=all")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 2)

    def test_cancel_stop_loss(self):
        record = _sl_row()
        session = FakeSession()
        session.register(StopLossOrder, first=record)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete("/api/trades/stop-loss/1")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "cancelled", "id": 1})
        self.assertEqual(record.status, "cancelled")

    def test_cancel_stop_loss_not_found(self):
        session = FakeSession()
        session.register(StopLossOrder, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete("/api/trades/stop-loss/1")

        self.assertEqual(resp.status_code, 404)

    def test_cancel_stop_loss_non_active(self):
        session = FakeSession()
        session.register(StopLossOrder, first=_sl_row(status="triggered"))
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete("/api/trades/stop-loss/1")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("triggered", resp.json()["detail"])


# ────────────── Take-profit ──────────────


def _tp_row(**overrides):
    base = {
        "id": 1,
        "token_id": "tok",
        "market_id": "mkt",
        "market_title": "Title",
        "outcome": "Yes",
        "size": 2.0,
        "take_profit_price": 0.8,
        "status": "active",
        "order_hash": None,
        "executed_price": None,
        "triggered_at": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TakeProfitTests(_TradesRouteTestCase):
    TP_BODY = {
        "token_id": "tok",
        "market_id": "mkt",
        "market_title": "Title",
        "outcome": "Yes",
        "size": 2.0,
        "take_profit_price": 0.8,
    }

    def test_set_take_profit_creates(self):
        session = FakeSession()
        session.register(TakeProfitOrder, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.post("/api/trades/take-profit", json=self.TP_BODY)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["take_profit_price"], 0.8)
        self.assertIsInstance(session.added[0], TakeProfitOrder)

    def test_set_take_profit_updates_existing(self):
        existing = _tp_row(take_profit_price=0.7, market_title="Old")
        session = FakeSession()
        session.register(TakeProfitOrder, first=existing)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.post("/api/trades/take-profit", json=self.TP_BODY)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["take_profit_price"], 0.8)
        self.assertEqual(data["market_title"], "Title")

    def test_get_take_profits(self):
        session = FakeSession()
        session.register(TakeProfitOrder, all_results=[_tp_row()])
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/take-profit?status=active")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_get_take_profits_all(self):
        session = FakeSession()
        session.register(TakeProfitOrder, all_results=[_tp_row(), _tp_row(id=2)])
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/take-profit?status=all")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 2)

    def test_cancel_take_profit(self):
        record = _tp_row()
        session = FakeSession()
        session.register(TakeProfitOrder, first=record)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete("/api/trades/take-profit/1")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "cancelled", "id": 1})
        self.assertEqual(record.status, "cancelled")

    def test_cancel_take_profit_not_found(self):
        session = FakeSession()
        session.register(TakeProfitOrder, first=None)
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete("/api/trades/take-profit/1")

        self.assertEqual(resp.status_code, 404)

    def test_cancel_take_profit_non_active(self):
        session = FakeSession()
        session.register(TakeProfitOrder, first=_tp_row(status="cancelled"))
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.delete("/api/trades/take-profit/1")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("cancelled", resp.json()["detail"])


# ────────────── Emergency stop ──────────────


class EmergencyStopTests(_TradesRouteTestCase):
    def test_emergency_stop_acknowledges_durable_work_without_signing(self):
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        report = {"operation_id": "op-1", "halted": True, "positions_confirmed_closed": 0}
        with (
            patch(
                "app.services.trading.trading_halt_service.request_halt",
                return_value=[SimpleNamespace(id="op-1")],
            ) as halt,
            patch(
                "app.services.trading.trading_halt_service.operation_status", return_value=report
            ),
            patch("app.services.trading.execution.live.place_live_order") as place,
        ):
            response = self.client.post("/api/trades/emergency-stop?close_positions=true")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), report)
        halt.assert_called_once_with(session, user_id=1, close_positions=True)
        place.assert_not_called()


class ResumeTradingTests(_TradesRouteTestCase):
    def test_resume_requires_verified_recovery(self):
        response = self.client.post("/api/trades/resume-trading")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "RECOVERY_REQUIRES_VERIFIED_EXECUTION_GATEWAY")


# ────────────── Trader quality / rescore ──────────────


class TraderQualityTests(_TradesRouteTestCase):
    def test_trader_quality_found(self):
        scores = {
            "quality_score": 85.0,
            "consistency_score": 70.0,
            "risk_adjusted_score": 60.0,
            "activity_score": 90.0,
            "win_rate_score": 75.0,
            "quality_tier": "A",
        }
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.trader_quality_service.get_trader_quality",
            return_value=scores,
        ):
            resp = self.client.get(f"/api/trades/trader/{TRADER.upper()}/quality")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["wallet_address"], TRADER)
        self.assertEqual(data["quality_score"], 85.0)
        self.assertEqual(data["quality_tier"], "A")

    def test_trader_quality_not_found(self):
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.trader_quality_service.get_trader_quality",
            return_value=None,
        ):
            resp = self.client.get(f"/api/trades/trader/{TRADER}/quality")

        self.assertEqual(resp.status_code, 404)


class RescoreTests(_TradesRouteTestCase):
    def test_rescore_all_traders(self):
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.trader_quality_service.score_all_traders",
            return_value=7,
        ):
            resp = self.client.post("/api/trades/traders/rescore")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"scored": 7, "message": "Quality scores updated for 7 traders"},
        )


# ────────────── Arbitrage ──────────────


class ArbitrageTests(_TradesRouteTestCase):
    def test_arbitrage_opportunities(self):
        opportunities = [
            {
                "type": "cross_market",
                "market_id": "m1",
                "market_title": "Market",
                "profit_pct": 5.0,
                "spread_pct": 1.0,
                "total_cost": 10.0,
                "bid": 0.4,
                "ask": 0.5,
                "token_id": "tok",
                "outcome": "Yes",
                "detected_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.arbitrage_service.get_recent_opportunities",
            return_value=opportunities,
        ):
            resp = self.client.get("/api/trades/arbitrage/opportunities")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["type"], "cross_market")

    def test_arbitrage_scan(self):
        opportunities = [
            {
                "type": "cross_market",
                "market_id": "m1",
                "market_title": "Market",
                "detected_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.arbitrage_service.scan_for_arbitrage",
            AsyncMock(return_value=opportunities),
        ):
            resp = self.client.post("/api/trades/arbitrage/scan")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["found"], 1)
        self.assertEqual(len(data["opportunities"]), 1)


# ────────────── Execution analytics ──────────────


class AnalyticsTests(_TradesRouteTestCase):
    def test_analytics_summary(self):
        summary = {
            "user_id": 1,
            "window_days": 7,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "per_strategy": {"copy": {"strategy": "copy"}},
            "totals": {"strategy": "all"},
            "data_quality": {"missing_fee": 0},
        }
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.execution_analytics.get_execution_summary",
            return_value=summary,
        ):
            resp = self.client.get("/api/trades/analytics/summary?window_days=7")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["window_days"], 7)
        self.assertEqual(data["totals"]["strategy"], "all")
        self.assertIn("copy", data["per_strategy"])

    def test_analytics_edge_scores(self):
        rows = [
            {
                "strategy": "copy",
                "edge_score": 55.0,
                "edge_raw": 0.1,
                "win_rate": 0.55,
                "avg_win": 10.0,
                "avg_loss": 8.0,
                "avg_fee": 0.1,
                "avg_risk_per_trade": 5.0,
                "filled": 10,
                "closed_trades": 10,
            }
        ]
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.execution_analytics.get_edge_scores",
            return_value=rows,
        ):
            resp = self.client.get("/api/trades/analytics/edge-score")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["user_id"], 1)
        self.assertEqual(data["window_days"], 30)
        self.assertEqual(data["strategies"][0]["strategy"], "copy")
        self.assertEqual(data["strategies"][0]["edge_score"], 55.0)
        self.assertIn("generated_at", data)

    def test_analytics_trade_details(self):
        trades = [
            {
                "id": 1,
                "market_id": "mkt",
                "token_id": "tok",
                "action": "buy",
                "strategy_source": "copy",
                "status": "executed",
                "expected_price": 0.5,
                "expected_size": 10.0,
                "filled_price": 0.55,
                "filled_size": 10.0,
                "fee_paid": 0.01,
                "slippage_bps": 100.0,
                "latency_ms": 250.0,
                "pnl": 1.0,
                "order_hash": "0xh",
                "executed_at": "2026-01-01T00:00:00+00:00",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.analytics.execution_analytics.get_trade_details",
            return_value=trades,
        ):
            resp = self.client.get("/api/trades/analytics/trades?strategy=copy&limit=10")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["strategy"], "copy")
        self.assertEqual(data["trades"][0]["slippage_bps"], 100.0)

    def test_analytics_trade_details_unknown_strategy(self):
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        resp = self.client.get("/api/trades/analytics/trades?strategy=bogus")

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown strategy 'bogus'", resp.json()["detail"])


# ────────────── Size suggestion ──────────────


class SizeSuggestionTests(_TradesRouteTestCase):
    def _suggest(self, session, **params):
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        return self.client.get("/api/trades/size-suggestion", params=params)

    def test_no_edge_estimate(self):
        session = FakeSession()
        session.register(UserSettings, first=None)
        with (
            patch.object(
                kelly_service, "resolve_edge_probability", MagicMock(return_value=(None, None))
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.5)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["capped_by"], "no_edge")
        self.assertEqual(data["suggested_size_usdc"], 0.0)
        self.assertIsNone(data["edge_probability"])
        self.assertIsNone(data["bankroll_usdc"])

    def test_price_fetch_failure(self):
        session = FakeSession()
        session.register(UserSettings, first=None)
        service = MagicMock()
        service.get_market_prices = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=service),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["capped_by"], "price_unavailable")
        self.assertEqual(data["edge_probability"], 0.6)
        self.assertEqual(data["edge_source"], "ai_assessment")

    def test_price_fetched_from_order_book(self):
        session = FakeSession()
        session.register(UserSettings, first=None)
        service = MagicMock()
        service.get_market_prices = AsyncMock(return_value={"yes_price": 0.55})
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch.object(
                kelly_service, "effective_trade_params", MagicMock(return_value=(0.6, 0.55))
            ),
            patch.object(kelly_service, "compute_bankroll", AsyncMock(return_value=1000.0)),
            patch.object(kelly_service, "size", MagicMock(return_value=50.0)),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=service),
            patch(
                "app.api.routes.trading.trades.load_wallet_credentials",
                return_value={"private_key": PRIVATE_KEY, "clob_creds": CLOB_CREDS},
            ),
            patch(
                "app.api.routes.trading.trades.apply_global_safety_caps",
                MagicMock(return_value=(50.0, None)),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["suggested_size_usdc"], 50.0)
        self.assertEqual(data["bankroll_usdc"], 1000.0)
        self.assertEqual(data["kelly_size_usdc"], 50.0)
        self.assertIsNone(data["capped_by"])
        service.get_market_prices.assert_awaited_once_with("mkt")

    def test_price_out_of_range(self):
        session = FakeSession()
        session.register(UserSettings, first=None)
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=1.0)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["capped_by"], "price_unavailable")

    def test_kelly_size_zero_capped_by_balance(self):
        session = FakeSession()
        session.register(UserSettings, first=None)
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch.object(
                kelly_service, "effective_trade_params", MagicMock(return_value=(0.6, 0.55))
            ),
            patch.object(kelly_service, "compute_bankroll", AsyncMock(return_value=0.0)),
            patch.object(kelly_service, "size", MagicMock(return_value=0.0)),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=MagicMock()),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=None),
            patch(
                "app.api.routes.trading.trades.apply_global_safety_caps",
                MagicMock(return_value=(0.0, None)),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.55)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["capped_by"], "balance")

    def test_capped_by_max_position_size(self):
        settings = SimpleNamespace(max_position_size=100.0, kelly_fraction=None)
        session = FakeSession()
        session.register(UserSettings, first=settings)
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch.object(
                kelly_service, "effective_trade_params", MagicMock(return_value=(0.6, 0.55))
            ),
            patch.object(kelly_service, "compute_bankroll", AsyncMock(return_value=1000.0)),
            patch.object(kelly_service, "size", MagicMock(return_value=100.0)),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=MagicMock()),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=None),
            patch(
                "app.api.routes.trading.trades.apply_global_safety_caps",
                MagicMock(return_value=(100.0, None)),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.55)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["capped_by"], "max_position_size")

    def test_capped_by_min_order(self):
        settings = SimpleNamespace(max_position_size=None, kelly_fraction=None)
        session = FakeSession()
        session.register(UserSettings, first=settings)
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch.object(
                kelly_service, "effective_trade_params", MagicMock(return_value=(0.6, 0.55))
            ),
            patch.object(kelly_service, "compute_bankroll", AsyncMock(return_value=1000.0)),
            patch.object(kelly_service, "size", MagicMock(return_value=0.5)),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=MagicMock()),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=None),
            patch(
                "app.api.routes.trading.trades.apply_global_safety_caps",
                MagicMock(return_value=(0.5, None)),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.55)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["capped_by"], "min_order")

    def test_capped_by_safety_caps(self):
        settings = SimpleNamespace(max_position_size=None, kelly_fraction=None)
        session = FakeSession()
        session.register(UserSettings, first=settings)
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch.object(
                kelly_service, "effective_trade_params", MagicMock(return_value=(0.6, 0.55))
            ),
            patch.object(kelly_service, "compute_bankroll", AsyncMock(return_value=1000.0)),
            patch.object(kelly_service, "size", MagicMock(return_value=50.0)),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=MagicMock()),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=None),
            patch(
                "app.api.routes.trading.trades.apply_global_safety_caps",
                MagicMock(return_value=(25.0, None)),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.55)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["capped_by"], "safety_caps")
        self.assertEqual(data["suggested_size_usdc"], 25.0)

    def test_rejected_by_safety_caps(self):
        settings = SimpleNamespace(max_position_size=None, kelly_fraction=None)
        session = FakeSession()
        session.register(UserSettings, first=settings)
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch.object(
                kelly_service, "effective_trade_params", MagicMock(return_value=(0.6, 0.55))
            ),
            patch.object(kelly_service, "compute_bankroll", AsyncMock(return_value=1000.0)),
            patch.object(kelly_service, "size", MagicMock(return_value=50.0)),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=MagicMock()),
            patch("app.api.routes.trading.trades.load_wallet_credentials", return_value=None),
            patch(
                "app.api.routes.trading.trades.apply_global_safety_caps",
                MagicMock(return_value=(0.0, "trading_halted")),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.55)

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["capped_by"], "trading_halted")
        self.assertEqual(data["suggested_size_usdc"], 0.0)
        self.assertEqual(data["kelly_size_usdc"], 50.0)

    def test_credential_store_error_uses_no_credentials(self):
        settings = SimpleNamespace(max_position_size=None, kelly_fraction=None)
        session = FakeSession()
        session.register(UserSettings, first=settings)
        compute_bankroll = AsyncMock(return_value=1000.0)
        with (
            patch.object(
                kelly_service,
                "resolve_edge_probability",
                MagicMock(return_value=(0.6, "ai_assessment")),
            ),
            patch.object(
                kelly_service, "effective_trade_params", MagicMock(return_value=(0.6, 0.55))
            ),
            patch.object(kelly_service, "compute_bankroll", compute_bankroll),
            patch.object(kelly_service, "size", MagicMock(return_value=50.0)),
            patch("app.api.routes.trading.trades.get_polymarket_service", return_value=MagicMock()),
            patch(
                "app.api.routes.trading.trades.load_wallet_credentials",
                side_effect=CredentialStoreError("down"),
            ),
            patch(
                "app.api.routes.trading.trades.apply_global_safety_caps",
                MagicMock(return_value=(50.0, None)),
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.55)

        self.assertEqual(resp.status_code, 200)
        compute_bankroll.assert_awaited_once()
        self.assertIsNone(compute_bankroll.call_args.kwargs["private_key"])
        self.assertIsNone(compute_bankroll.call_args.kwargs["clob_creds"])

    def test_settings_created_when_missing(self):
        session = FakeSession()
        session.register(UserSettings, first=None)
        with (
            patch.object(
                kelly_service, "resolve_edge_probability", MagicMock(return_value=(None, None))
            ),
        ):
            resp = self._suggest(session, market_id="mkt", side="BUY", price=0.5)

        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(session.added[0], UserSettings)


# ────────────── Per-trader copy attribution (plan 07) ──────────────


class CopyAttributionTests(_TradesRouteTestCase):
    def _attribution(self, **overrides):
        payload = {
            "window_days": 30,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "traders": [
                {
                    "wallet": TRADER,
                    "label": "Sharp Wallet",
                    "realized_pnl": 20.0,
                    "trade_count": 2,
                    "wins": 1,
                    "losses": 0,
                    "win_rate": 1.0,
                    "volume_usdc": 120.0,
                    "open_positions": 1,
                    "last_trade_at": "2026-01-01T00:00:00+00:00",
                }
            ],
            "totals": {"realized_pnl": 20.0, "trade_count": 2, "win_rate": 1.0},
            "data_quality": {"unreconciled_sells": 1},
        }
        payload.update(overrides)
        return payload

    def _get(self, params=""):
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        return self.client.get(f"/api/trades/copy-trades/pnl/by-trader{params}")

    def test_attribution_defaults_to_30_days(self):
        with patch(
            "app.services.trading.copy_trade_service.get_trader_copy_stats",
            return_value=self._attribution(),
        ) as stats:
            resp = self._get()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), self._attribution())
        self.assertEqual(stats.call_args.args[1], 1)
        self.assertEqual(stats.call_args.kwargs["window_days"], 30)

    def test_attribution_window_days_forwarded(self):
        empty_totals = {"realized_pnl": 0.0, "trade_count": 0, "win_rate": None}
        with patch(
            "app.services.trading.copy_trade_service.get_trader_copy_stats",
            return_value=self._attribution(window_days=90, traders=[], totals=empty_totals),
        ):
            resp = self._get("?window_days=90")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["window_days"], 90)
        self.assertEqual(data["traders"], [])
        self.assertIsNone(data["totals"]["win_rate"])

    def test_attribution_rejects_out_of_range_window(self):
        self.assertEqual(self._get("?window_days=0").status_code, 422)
        self.assertEqual(self._get("?window_days=366").status_code, 422)

    def test_attribution_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        self.addCleanup(app.dependency_overrides.pop, get_current_user_from_token, None)
        session = FakeSession()
        app.dependency_overrides[get_db] = lambda: session
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        with patch(
            "app.services.trading.copy_trade_service.get_trader_copy_stats",
            return_value=self._attribution(),
        ):
            resp = self.client.get("/api/trades/copy-trades/pnl/by-trader")

        self.assertEqual(resp.status_code, 401)

    def test_daily_copy_pnl_response_is_unchanged(self):
        with patch(
            "app.api.routes.trading.trades.get_daily_copy_pnl",
            AsyncMock(return_value=-12.5),
        ):
            resp = self.client.get("/api/trades/copy-trades/pnl")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"daily_pnl": -12.5})


if __name__ == "__main__":
    unittest.main()
