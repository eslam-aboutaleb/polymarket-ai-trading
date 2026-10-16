"""Unit tests for per-trader copy attribution (plan 07).

``get_trader_copy_stats`` answers "which copied trader actually made me
money?", so every number it reports has to be traceable to rows that
really happened:

* the rolling window filters on ``executed_at`` (PnL is written on the
  closing sell, never on ``created_at``);
 * NULL ``pnl`` rows are excluded from the PnL sums — never coerced to 0 —
   with unrealized sells and undated copy trades surfaced in
   ``data_quality`` (``unreconciled_sells`` / ``missing_executed_at``);
* ``FollowedTrader.trader_alias`` wins over the truncated wallet, and
  wallet casing never splits one trader into two rows.

A real in-memory SQLite session is used (pattern copied from
``test_pnl_reconciliation.py``) so the SQL-side aggregation is genuinely
exercised rather than mocked. ``UserTrade.pnl`` is only ever read here.
"""

import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.main import app
from app.models.base import Base
from app.models.followed_trader import FollowedTrader
from app.models.user_trade import UserTrade
from app.services.trading.copy_trade_service import get_trader_copy_stats
from app.utils.database import get_db

WALLET_A = "0x" + "aa" * 20
WALLET_B = "0x" + "bb" * 20
WALLET_C = "0x" + "cc" * 20


class TraderAttributionTestBase(unittest.TestCase):
    """Shared in-memory database with the tables attribution reads."""

    def setUp(self):
        # StaticPool + check_same_thread=False: TestClient runs the route in a
        # worker thread while the fixture seeds the same in-memory database.
        self.engine = create_engine(
            "sqlite://",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            self.engine,
            tables=[UserTrade.__table__, FollowedTrader.__table__],
        )
        self.db = sessionmaker(bind=self.engine, future=True)()
        # addCleanup is LIFO: the engine is disposed after the session closes.
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.now = datetime.now(UTC)

    def _trade(self, action="buy", wallet=WALLET_A, price=0.5, amount=50.0, **kwargs):
        """Insert one UserTrade row that landed ``age_days`` ago."""
        values = {
            "user_id": kwargs.pop("user_id", 1),
            "market_id": "mkt-a",
            "token_id": "tok-a",
            "action": action,
            "amount": amount,
            "price": price,
            "status": kwargs.pop("status", "executed"),
            "executed_at": self.now - timedelta(days=kwargs.pop("age_days", 1)),
            "copied_from_wallet": wallet,
            "pnl": None,
        }
        values.update(kwargs)
        trade = UserTrade(**values)
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def _alias(self, wallet, alias, user_id=1):
        self.db.add(
            FollowedTrader(
                user_id=user_id,
                trader_wallet=wallet,
                trader_alias=alias,
                is_active=True,
                copy_wallet_percentage=100.0,
            ),
        )
        self.db.commit()

    def _by_wallet(self, stats):
        return {row["wallet"]: row for row in stats["traders"]}


class WindowTests(TraderAttributionTestBase):
    def test_trades_older_than_the_window_are_excluded(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0, age_days=90)
        self._trade(wallet=WALLET_B, action="sell", amount=40.0, pnl=-10.0, age_days=10)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)
        wallets = set(self._by_wallet(stats))

        self.assertEqual(wallets, {WALLET_B.lower()})
        self.assertEqual(stats["totals"]["realized_pnl"], -10.0)

    def test_window_is_measured_on_executed_at_not_created_at(self):
        # A buy opened long ago and still open at the window edge carries
        # no PnL: it must not read as a loss.
        buy = self._trade(wallet=WALLET_A, amount=50.0, age_days=45)
        self._trade(
            wallet=WALLET_A,
            action="sell",
            amount=70.0,
            price=0.7,
            pnl=20.0,
            age_days=1,
        )
        buy.created_at = self.now - timedelta(days=45)
        self.db.commit()

        stats = get_trader_copy_stats(self.db, 1, window_days=30)
        row = self._by_wallet(stats)[WALLET_A.lower()]

        self.assertEqual(row["trade_count"], 1)
        self.assertEqual(row["realized_pnl"], 20.0)
        self.assertEqual(row["open_positions"], 0)
        self.assertEqual(stats["totals"]["realized_pnl"], 20.0)

    def test_window_days_floored_at_one(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=5.0, age_days=1)

        self.assertEqual(get_trader_copy_stats(self.db, 1, window_days=0)["window_days"], 1)
        self.assertEqual(get_trader_copy_stats(self.db, 1, window_days=-5)["window_days"], 1)

    def test_window_defaults_to_the_configured_setting(self):
        self.assertEqual(
            get_trader_copy_stats(self.db, 1)["window_days"],
            get_settings().copy_attribution_window_days,
        )


class PnlAttributionTests(TraderAttributionTestBase):
    def test_wins_losses_and_win_rate_per_trader(self):
        # 100 shares at 0.50 sold at 0.70 → +20; at 0.40 → −10.
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, price=0.7, pnl=20.0)
        self._trade(wallet=WALLET_A, action="sell", amount=40.0, price=0.4, pnl=-10.0)
        self._trade(wallet=WALLET_A, action="sell", amount=60.0, price=0.6, pnl=0.0)
        self._trade(wallet=WALLET_B, action="sell", amount=55.0, price=0.55, pnl=5.0)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)
        row_a = self._by_wallet(stats)[WALLET_A.lower()]

        # A break-even close (pnl == 0) is neither a win nor a loss.
        self.assertEqual((row_a["wins"], row_a["losses"]), (1, 1))
        self.assertEqual(row_a["win_rate"], 0.5)
        self.assertEqual(row_a["realized_pnl"], 10.0)
        self.assertEqual(row_a["volume_usdc"], 170.0)
        self.assertEqual(row_a["trade_count"], 3)
        # Totals also count trader B's single win: 2 wins / 1 loss.
        self.assertEqual(stats["totals"]["win_rate"], round(2 / 3, 4))

    def test_win_rate_is_null_until_a_trade_closes(self):
        self._trade(wallet=WALLET_A, amount=50.0)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)
        row = self._by_wallet(stats)[WALLET_A.lower()]

        self.assertEqual(row["realized_pnl"], 0.0)
        self.assertIsNone(row["win_rate"])
        self.assertIsNone(stats["totals"]["win_rate"])
        self.assertEqual(row["open_positions"], 1)

    def test_open_positions_counts_only_unreconciled_buys(self):
        self._trade(wallet=WALLET_A, action="buy", amount=50.0, pnl=None)
        self._trade(wallet=WALLET_A, action="buy", amount=30.0, pnl=12.0)
        # A closed sell is not an open position, and a NULL-pnl sell is not
        # an open position either (it is an unreconciled sell).
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)
        self._trade(wallet=WALLET_A, action="sell", amount=10.0, pnl=None)

        row = self._by_wallet(get_trader_copy_stats(self.db, 1, window_days=30))[WALLET_A.lower()]

        self.assertEqual(row["open_positions"], 1)
        self.assertEqual(row["realized_pnl"], 32.0)

    def test_null_pnl_sellers_are_excluded_and_surfaced_as_data_quality(self):
        # Realized sells reconcile normally; the third sell never did.
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)
        self._trade(wallet=WALLET_A, action="sell", amount=40.0, pnl=-10.0)
        self._trade(wallet=WALLET_A, action="sell", amount=25.0, pnl=None)
        # A failed sell never reached the exchange, so it is not a gap.
        self._trade(wallet=WALLET_A, action="sell", amount=25.0, pnl=None, status="failed")
        # A pending buy is not an unreconciled sell either.
        self._trade(wallet=WALLET_A, action="buy", amount=25.0, pnl=None, status="pending")

        stats = get_trader_copy_stats(self.db, 1, window_days=30)
        row = self._by_wallet(stats)[WALLET_A.lower()]

        self.assertEqual(row["realized_pnl"], 10.0)
        self.assertEqual(
            stats["data_quality"],
            {"unreconciled_sells": 1, "missing_executed_at": 0},
        )

    def test_traders_are_sorted_by_realized_pnl_desc(self):
        self._trade(wallet=WALLET_B, action="sell", amount=70.0, pnl=-4.0)
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=30.0)
        self._trade(wallet=WALLET_C, action="sell", amount=70.0, pnl=12.0)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(
            [row["wallet"] for row in stats["traders"]],
            [WALLET_A.lower(), WALLET_C.lower(), WALLET_B.lower()],
        )

    def test_non_copy_trades_are_never_attributed(self):
        self._trade(wallet=None, action="sell", amount=70.0, pnl=20.0)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(stats["traders"], [])
        self.assertEqual(stats["totals"]["realized_pnl"], 0.0)
        self.assertEqual(stats["totals"]["trade_count"], 0)
        self.assertEqual(
            stats["data_quality"],
            {"unreconciled_sells": 0, "missing_executed_at": 0},
        )

    def test_other_users_trades_are_not_visible(self):
        self._trade(wallet=WALLET_A, user_id=2, action="sell", amount=70.0, pnl=20.0)
        self._trade(wallet=WALLET_A, user_id=1, action="sell", amount=70.0, pnl=5.0)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(stats["totals"]["realized_pnl"], 5.0)
        self.assertEqual(stats["totals"]["trade_count"], 1)

    def test_mixed_case_wallets_are_one_trader(self):
        self._trade(wallet=WALLET_A.upper(), action="sell", amount=70.0, pnl=20.0)
        self._trade(wallet=WALLET_A.lower(), action="buy", amount=50.0, pnl=None)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)
        rows = stats["traders"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["realized_pnl"], 20.0)
        self.assertEqual(rows[0]["trade_count"], 2)
        self.assertEqual(rows[0]["open_positions"], 1)
        self.assertEqual(rows[0]["volume_usdc"], 120.0)

    def test_pnl_is_never_written(self):
        trade = self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)

        get_trader_copy_stats(self.db, 1, window_days=30)

        self.db.refresh(trade)
        self.assertEqual(trade.pnl, 20.0)


class LabelTests(TraderAttributionTestBase):
    def test_alias_preferred_over_truncated_wallet(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)
        self._alias(WALLET_A, "  Sharp Wallet  ")

        row = self._by_wallet(get_trader_copy_stats(self.db, 1, window_days=30))[WALLET_A.lower()]

        self.assertEqual(row["label"], "Sharp Wallet")
        self.assertEqual(row["wallet"], WALLET_A.lower())

    def test_blank_alias_falls_back_to_truncated_wallet(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)
        self._alias(WALLET_A, "   ")

        row = self._by_wallet(get_trader_copy_stats(self.db, 1, window_days=30))[WALLET_A.lower()]

        self.assertEqual(row["label"], f"{WALLET_A[:6]}...{WALLET_A[-4:]}")

    def test_missing_alias_falls_back_to_truncated_wallet(self):
        self._trade(wallet=WALLET_B, action="sell", amount=70.0, pnl=20.0)

        row = self._by_wallet(get_trader_copy_stats(self.db, 1, window_days=30))[WALLET_B.lower()]

        self.assertEqual(row["label"], f"{WALLET_B[:6]}...{WALLET_B[-4:]}")

    def test_another_users_alias_is_not_reused(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)
        self._alias(WALLET_A, "Someone Else", user_id=2)

        row = self._by_wallet(get_trader_copy_stats(self.db, 1, window_days=30))[WALLET_A.lower()]

        self.assertNotEqual(row["label"], "Someone Else")


class LastTradeTests(TraderAttributionTestBase):
    def test_last_trade_at_is_the_newest_trade_in_the_window(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=1.0, age_days=9)
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=2.0, age_days=2)

        row = self._by_wallet(get_trader_copy_stats(self.db, 1, window_days=30))[WALLET_A.lower()]

        # SQLite round-trips DateTime columns as naive UTC datetimes.
        expected = (self.now - timedelta(days=2)).replace(tzinfo=None, microsecond=0)
        actual = datetime.fromisoformat(row["last_trade_at"]).replace(microsecond=0)
        self.assertEqual(actual, expected)

    def test_last_trade_at_is_null_without_executed_trades(self):
        self._trade(wallet=WALLET_A, action="buy", amount=50.0, pnl=None, executed_at=None)

        row = self._by_wallet(get_trader_copy_stats(self.db, 1, window_days=30))

        self.assertEqual(row, {})
        # …and it must not disappear silently: the window filters on
        # executed_at, so this row is invisible to every figure and is
        # reported through the data-quality counter instead.
        stats = get_trader_copy_stats(self.db, 1, window_days=30)
        self.assertEqual(stats["data_quality"]["missing_executed_at"], 1)


class TotalsTests(TraderAttributionTestBase):
    def test_totals_sum_every_trader(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)
        self._trade(wallet=WALLET_A, action="sell", amount=40.0, pnl=-5.0)
        self._trade(wallet=WALLET_B, action="sell", amount=55.0, pnl=5.0)
        self._trade(wallet=WALLET_C, action="buy", amount=50.0)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(stats["totals"]["realized_pnl"], 20.0)
        self.assertEqual(stats["totals"]["trade_count"], 4)
        # wins = 2 (20, 5); losses = 1 (−5); the open buy is not counted.
        self.assertEqual(stats["totals"]["win_rate"], round(2 / 3, 4))

    def test_payload_shape(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(
            set(stats),
            {"window_days", "generated_at", "traders", "totals", "data_quality"},
        )
        self.assertEqual(set(stats["totals"]), {"realized_pnl", "trade_count", "win_rate"})
        self.assertEqual(
            set(stats["traders"][0]),
            {
                "wallet",
                "label",
                "realized_pnl",
                "trade_count",
                "wins",
                "losses",
                "win_rate",
                "volume_usdc",
                "open_positions",
                "last_trade_at",
            },
        )


class EmptyStateTests(TraderAttributionTestBase):
    def test_no_copy_history(self):
        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(stats["traders"], [])
        self.assertEqual(stats["totals"]["realized_pnl"], 0.0)
        self.assertEqual(stats["totals"]["trade_count"], 0)
        self.assertIsNone(stats["totals"]["win_rate"])
        self.assertEqual(
            stats["data_quality"],
            {"unreconciled_sells": 0, "missing_executed_at": 0},
        )

    def test_window_before_any_copy_trade(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0, age_days=30)

        stats = get_trader_copy_stats(self.db, 1, window_days=7)

        self.assertEqual(stats["traders"], [])


class MissingExecutedAtTests(TraderAttributionTestBase):
    """``data_quality.missing_executed_at`` keeps the ghost rows visible.

    The cohort and the unreconciled-sell count both require
    ``executed_at >= cutoff``, so a copy trade with no fill timestamp is
    invisible to every figure. Without the extra counter a user whose copy
    trades are all like that sees an empty table that reads exactly like
    "no copy trades".
    """

    def test_copy_trades_without_a_timestamp_are_counted_not_shown(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0, executed_at=None)
        self._trade(wallet=WALLET_B, action="buy", amount=30.0, executed_at=None)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(stats["traders"], [])
        self.assertEqual(stats["totals"]["trade_count"], 0)
        # No side or status filter: a row whose fill timestamp was never
        # recorded is a gap whichever way it traded.
        self.assertEqual(stats["data_quality"]["missing_executed_at"], 2)

    def test_the_counter_ignores_non_copy_trades_and_other_users(self):
        self._trade(wallet=None, action="sell", amount=70.0, pnl=20.0, executed_at=None)
        self._trade(wallet=WALLET_A, action="sell", amount=10.0, user_id=2, executed_at=None)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(stats["data_quality"]["missing_executed_at"], 0)

    def test_dated_copy_trades_are_not_counted(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0, age_days=1)

        stats = get_trader_copy_stats(self.db, 1, window_days=30)

        self.assertEqual(stats["data_quality"]["missing_executed_at"], 0)
        self.assertEqual(len(stats["traders"]), 1)

    def test_the_counter_survives_an_old_row_outside_the_window(self):
        # The window does not apply: an undated row has no window to fall in,
        # so it is always reported rather than only for recent activity.
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0, executed_at=None)

        stats = get_trader_copy_stats(self.db, 1, window_days=1)

        self.assertEqual(stats["data_quality"]["missing_executed_at"], 1)


class RouteIntegrationTests(TraderAttributionTestBase):
    """End-to-end: seeded rows → service → route → JSON payload."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": "0x" + "ef" * 20,
            "is_admin": False,
        }
        app.dependency_overrides[get_db] = lambda: self.db
        self.addCleanup(app.dependency_overrides.pop, get_current_user_from_token, None)
        self.addCleanup(app.dependency_overrides.pop, get_db, None)

    def test_by_trader_payload_matches_the_seeded_book(self):
        self._trade(wallet=WALLET_A, action="buy", amount=50.0, pnl=None)
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, price=0.7, pnl=20.0)
        self._trade(wallet=WALLET_B, action="sell", amount=40.0, price=0.4, pnl=-10.0)
        self._alias(WALLET_A, "Sharp Wallet")

        resp = self.client.get("/api/trades/copy-trades/pnl/by-trader?window_days=30")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["window_days"], 30)
        self.assertEqual(
            [row["label"] for row in data["traders"]],
            ["Sharp Wallet", f"{WALLET_B[:6]}...{WALLET_B[-4:]}"],
        )
        self.assertEqual(data["traders"][0]["realized_pnl"], 20.0)
        self.assertEqual(data["traders"][1]["realized_pnl"], -10.0)
        self.assertEqual(data["traders"][0]["open_positions"], 1)
        self.assertEqual(data["totals"], {"realized_pnl": 10.0, "trade_count": 3, "win_rate": 0.5})
        self.assertEqual(
            data["data_quality"],
            {"unreconciled_sells": 0, "missing_executed_at": 0},
        )

    def test_by_trader_defaults_to_30_days(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=5.0, age_days=10)

        resp = self.client.get("/api/trades/copy-trades/pnl/by-trader")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["window_days"], 30)

    def test_window_out_of_range_is_rejected(self):
        for window in (0, 366):
            resp = self.client.get(f"/api/trades/copy-trades/pnl/by-trader?window_days={window}")
            self.assertEqual(resp.status_code, 422, window)

    def test_missing_executed_at_is_surfaced_in_the_payload(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=20.0, executed_at=None)

        resp = self.client.get("/api/trades/copy-trades/pnl/by-trader")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data_quality"]["missing_executed_at"], 1)
        self.assertEqual(resp.json()["traders"], [])

    def test_token_without_user_id_fails_loudly(self):
        """A token payload missing ``user_id`` must not look like an empty book."""
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "wallet_address": "0x" + "ef" * 20
        }
        self.addCleanup(
            app.dependency_overrides.__setitem__,
            get_current_user_from_token,
            lambda: {"user_id": 1, "wallet_address": "0x" + "ef" * 20, "is_admin": False},
        )
        with self.assertRaises(KeyError):
            self.client.get("/api/trades/copy-trades/pnl/by-trader")

    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user_from_token, None)
        resp = self.client.get("/api/trades/copy-trades/pnl/by-trader")

        self.assertEqual(resp.status_code, 401)

    def test_daily_pnl_endpoint_shape_is_unchanged(self):
        self._trade(wallet=WALLET_A, action="sell", amount=70.0, pnl=12.0, age_days=0)

        resp = self.client.get("/api/trades/copy-trades/pnl")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"daily_pnl": 12.0})


if __name__ == "__main__":
    unittest.main()
