"""Coverage tests for the plan-05 equity-curve endpoints in portfolio.py.

Follows the ``test_portfolio_routes_coverage.py`` pattern: a real
in-memory SQLite session behind the ``get_db`` dependency override and an
auth override, with a ``TestClient`` on the real ``app``. No wallet
calls are made on the read path, so nothing needs mocking there.
"""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.portfolio.portfolio import _equity_window, _parse_moment
from app.main import app
from app.models.analytics.portfolio_snapshot import PortfolioSnapshot
from app.models.users.user import User
from app.services.analytics import equity_snapshot_service as svc
from app.utils.database import get_db
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20


class EquityRouteTestBase(unittest.TestCase):
    """Shared fixtures: in-memory DB with the snapshots table, auth override."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        User.__table__.create(self.engine)
        PortfolioSnapshot.__table__.create(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )
        self.db = self.session_factory()

        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        app.dependency_overrides[get_db] = self._get_db_override

        self.client = TestClient(app)
        self.addCleanup(self._cleanup)

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _cleanup(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def _snapshot(self, user_id=1, bankroll=100.0, usdc=None, age_hours=0, is_paper=False):
        captured = utc_now() - timedelta(hours=age_hours)
        row = PortfolioSnapshot(
            user_id=user_id,
            bucket_hour=svc.truncate_to_hour(captured),
            captured_at=captured,
            usdc_balance=usdc if usdc is not None else bankroll - 2.0,
            unrealized_pnl=0.0 if is_paper else 2.0,
            bankroll=bankroll,
            total_invested=10.0,
            total_current_value=12.0,
            positions_count=1,
            is_paper=is_paper,
        )
        self.db.add(row)
        return row

    def _seed(self, *rows):
        for row in rows:
            self.db.add(row)
        self.db.commit()

    def _day_row(self, bankroll, moment, hour=14):
        """A row pinned to a specific UTC calendar day (and hour of that day)."""
        self._seed(
            PortfolioSnapshot(
                user_id=1,
                bucket_hour=moment.replace(hour=hour, minute=0, second=0, microsecond=0),
                captured_at=moment,
                usdc_balance=bankroll,
                unrealized_pnl=0.0,
                bankroll=bankroll,
                total_invested=0.0,
                total_current_value=0.0,
                positions_count=0,
                is_paper=False,
            )
        )


class EquitySnapshotsRouteTests(EquityRouteTestBase):
    def test_returns_ascending_series_for_current_user(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=3),
            self._snapshot(bankroll=110.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/snapshots")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual([row["bankroll"] for row in data], [100.0, 110.0])
        self.assertEqual(data[0]["usdc_balance"], 98.0)
        self.assertEqual(data[0]["unrealized_pnl"], 2.0)
        self.assertEqual(data[0]["positions_count"], 1)
        self.assertEqual(data[0]["total_invested"], 10.0)
        self.assertEqual(data[0]["total_current_value"], 12.0)
        self.assertFalse(data[0]["is_paper"])
        self.assertIn("captured_at", data[0])
        self.assertIn("bucket_hour", data[0])

    def test_scopes_to_the_authenticated_user(self):
        self._seed(
            self._snapshot(user_id=1, bankroll=100.0),
            self._snapshot(user_id=2, bankroll=900.0),
        )
        resp = self.client.get("/api/portfolio/equity/snapshots")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["bankroll"] for row in resp.json()], [100.0])

    def test_empty_history_returns_an_empty_list_not_404(self):
        resp = self.client.get("/api/portfolio/equity/snapshots")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_default_window_is_the_last_seven_days(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=200),
            self._snapshot(bankroll=110.0, age_hours=10),
        )
        resp = self.client.get("/api/portfolio/equity/snapshots")
        self.assertEqual([row["bankroll"] for row in resp.json()], [110.0])

    def test_from_and_to_bound_the_window(self):
        now = utc_now()
        with patch.object(svc, "utc_now", return_value=now):
            self._seed(
                self._snapshot(bankroll=100.0, age_hours=10),
                self._snapshot(bankroll=110.0, age_hours=5),
                self._snapshot(bankroll=120.0, age_hours=1),
            )
        start = (now - timedelta(hours=6)).isoformat()
        end = (now - timedelta(hours=2)).isoformat()
        resp = self.client.get("/api/portfolio/equity/snapshots", params={"from": start, "to": end})
        self.assertEqual([row["bankroll"] for row in resp.json()], [110.0])

    def test_limit_keeps_the_newest_points_in_order(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=3),
            self._snapshot(bankroll=110.0, age_hours=2),
            self._snapshot(bankroll=120.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/snapshots", params={"limit": 2})
        self.assertEqual([row["bankroll"] for row in resp.json()], [110.0, 120.0])

    def test_date_selects_one_utc_calendar_day(self):
        now = utc_now()
        # Yesterday's 14:00 sample must be excluded; today's 14:00 kept.
        self._day_row(bankroll=200.0, moment=now - timedelta(days=1))
        self._day_row(bankroll=100.0, moment=now)

        resp = self.client.get(
            "/api/portfolio/equity/snapshots",
            params={"date": now.date().isoformat()},
        )
        self.assertEqual([row["bankroll"] for row in resp.json()], [100.0])

    def test_naive_timestamps_are_treated_as_utc(self):
        self._seed(self._snapshot(bankroll=100.0, age_hours=1))
        start = (utc_now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        resp = self.client.get("/api/portfolio/equity/snapshots", params={"from": start})
        self.assertEqual([row["bankroll"] for row in resp.json()], [100.0])

    def test_unparseable_window_falls_back_to_the_default(self):
        """A bad ``from`` is ignored, not a 500 — the default window is used."""
        self._seed(self._snapshot(bankroll=100.0, age_hours=1))
        resp = self.client.get("/api/portfolio/equity/snapshots", params={"from": "not-a-date"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["bankroll"] for row in resp.json()], [100.0])

    def test_an_explicit_old_from_overrides_the_seven_day_default(self):
        """The "all" period sends an epoch lower bound; it must be honoured.

        The default window is 7 days, so without this the panel's "All"
        selector plots 168 points while the summary beside it reports every
        point ever written — two different answers to the same question.
        """
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=24 * 400),
            self._snapshot(bankroll=150.0, age_hours=24 * 30),
            self._snapshot(bankroll=175.0, age_hours=1),
        )
        resp = self.client.get(
            "/api/portfolio/equity/snapshots",
            params={"from": datetime(1970, 1, 1, tzinfo=UTC).isoformat(), "limit": 2000},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([row["bankroll"] for row in resp.json()], [100.0, 150.0, 175.0])

    def test_is_paper_false_returns_only_live_rows(self):
        self._seed(
            self._snapshot(bankroll=1000.0, is_paper=True),
            self._snapshot(bankroll=100.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/snapshots", params={"is_paper": "false"})

        self.assertEqual([row["bankroll"] for row in resp.json()], [100.0])
        self.assertFalse(resp.json()[0]["is_paper"])

    def test_is_paper_true_returns_only_paper_rows(self):
        self._seed(
            self._snapshot(bankroll=1000.0, is_paper=True),
            self._snapshot(bankroll=100.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/snapshots", params={"is_paper": "true"})

        data = resp.json()
        self.assertEqual([row["bankroll"] for row in data], [1000.0])
        self.assertTrue(data[0]["is_paper"])

    def test_is_paper_omitted_returns_both_accounts(self):
        """The unfiltered escape hatch stays available for callers that pair it."""
        self._seed(
            self._snapshot(bankroll=1000.0, is_paper=True),
            self._snapshot(bankroll=100.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/snapshots")

        self.assertEqual(len(resp.json()), 2)

    def test_paper_rows_are_returned_with_the_flag(self):
        self._seed(self._snapshot(bankroll=1000.0, is_paper=True))
        resp = self.client.get("/api/portfolio/equity/snapshots")
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertTrue(data[0]["is_paper"])

    def test_requires_authentication(self):
        app.dependency_overrides.clear()
        resp = self.client.get("/api/portfolio/equity/snapshots")
        self.assertEqual(resp.status_code, 401)


class EquitySummaryRouteTests(EquityRouteTestBase):
    def test_summary_math_with_max_drawdown(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=10),
            self._snapshot(bankroll=80.0, age_hours=8),
            self._snapshot(bankroll=60.0, age_hours=6),
            self._snapshot(bankroll=120.0, age_hours=4),
        )
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "7d"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["period"], "7d")
        self.assertEqual(data["start"], 100.0)
        self.assertEqual(data["end"], 120.0)
        self.assertEqual(data["delta"], 20.0)
        self.assertEqual(data["delta_pct"], 20.0)
        self.assertAlmostEqual(data["max_drawdown_pct"], 40.0)
        self.assertEqual(data["points"], 4)

    def test_empty_history_is_zeros_not_404(self):
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "24h"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "period": "24h",
                "start": 0.0,
                "end": 0.0,
                "delta": 0.0,
                "delta_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "points": 0,
            },
        )

    def test_24h_period_excludes_older_points(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=72),
            self._snapshot(bankroll=200.0, age_hours=2),
        )
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "24h"})
        data = resp.json()
        self.assertEqual(data["points"], 1)
        self.assertEqual(data["start"], 200.0)
        self.assertEqual(data["delta"], 0.0)

    def test_all_period_keeps_every_point(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=1000),
            self._snapshot(bankroll=150.0, age_hours=500),
            self._snapshot(bankroll=90.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "all"})
        data = resp.json()
        self.assertEqual(data["points"], 3)
        self.assertEqual(data["start"], 100.0)
        self.assertEqual(data["end"], 90.0)
        self.assertEqual(data["delta"], -10.0)
        self.assertEqual(data["delta_pct"], -10.0)
        # Peak 150 → trough 90 = -40%.
        self.assertAlmostEqual(data["max_drawdown_pct"], 40.0)

    def test_is_paper_false_summarises_only_the_live_account(self):
        # A user who toggled simulation_mode has both accounts: 1000 live →
        # 100 live, and 500 paper. A summary that spans both reports a delta
        # between two different bankrolls, so the mode is filtered explicitly.
        self._seed(
            self._snapshot(bankroll=1000.0, age_hours=4),
            self._snapshot(bankroll=500.0, age_hours=3, is_paper=True),
            self._snapshot(bankroll=100.0, age_hours=2),
            self._snapshot(bankroll=600.0, age_hours=1, is_paper=True),
        )
        resp = self.client.get(
            "/api/portfolio/equity/summary", params={"period": "7d", "is_paper": "false"}
        )

        data = resp.json()
        self.assertEqual(data["points"], 2)
        self.assertEqual(data["start"], 1000.0)
        self.assertEqual(data["end"], 100.0)
        self.assertEqual(data["delta"], -900.0)

    def test_is_paper_true_summarises_only_the_paper_account(self):
        self._seed(
            self._snapshot(bankroll=1000.0, age_hours=4),
            self._snapshot(bankroll=500.0, age_hours=3, is_paper=True),
            self._snapshot(bankroll=100.0, age_hours=2),
            self._snapshot(bankroll=600.0, age_hours=1, is_paper=True),
        )
        resp = self.client.get(
            "/api/portfolio/equity/summary", params={"period": "7d", "is_paper": "true"}
        )

        data = resp.json()
        self.assertEqual(data["points"], 2)
        self.assertEqual(data["start"], 500.0)
        self.assertEqual(data["end"], 600.0)
        self.assertEqual(data["delta"], 100.0)

    def test_is_paper_true_with_only_live_history_is_zeros_not_an_error(self):
        self._seed(self._snapshot(bankroll=100.0, age_hours=2))
        resp = self.client.get(
            "/api/portfolio/equity/summary", params={"period": "7d", "is_paper": "true"}
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["points"], 0)

    def test_zero_start_does_not_divide_by_zero(self):
        self._seed(self._snapshot(bankroll=0.0, age_hours=2))
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "7d"})
        data = resp.json()
        self.assertEqual(data["start"], 0.0)
        self.assertEqual(data["delta_pct"], 0.0)

    def test_rising_series_has_no_drawdown(self):
        self._seed(
            self._snapshot(bankroll=50.0, age_hours=3),
            self._snapshot(bankroll=60.0, age_hours=2),
            self._snapshot(bankroll=70.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "7d"})
        self.assertEqual(resp.json()["max_drawdown_pct"], 0.0)

    def test_scopes_to_the_authenticated_user(self):
        self._seed(
            self._snapshot(bankroll=100.0),
            self._snapshot(user_id=2, bankroll=900.0),
        )
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "7d"})
        data = resp.json()
        self.assertEqual(data["points"], 1)
        self.assertEqual(data["end"], 100.0)

    def test_unknown_period_falls_back_to_7d(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=200),
            self._snapshot(bankroll=200.0, age_hours=1),
        )
        resp = self.client.get("/api/portfolio/equity/summary", params={"period": "bogus"})
        data = resp.json()
        self.assertEqual(data["period"], "bogus")
        self.assertEqual(data["points"], 1)
        self.assertEqual(data["end"], 200.0)

    def test_default_period_is_7d(self):
        self._seed(
            self._snapshot(bankroll=100.0, age_hours=200),
            self._snapshot(bankroll=200.0, age_hours=100),
        )
        resp = self.client.get("/api/portfolio/equity/summary")
        data = resp.json()
        self.assertEqual(data["period"], "7d")
        self.assertEqual(data["points"], 1)
        self.assertEqual(data["end"], 200.0)

    def test_requires_authentication(self):
        app.dependency_overrides.clear()
        resp = self.client.get("/api/portfolio/equity/summary")
        self.assertEqual(resp.status_code, 401)


class EquityWindowHelperTests(unittest.TestCase):
    """The ``?from``/``?to``/``?date`` window parser, in isolation."""

    def test_parse_moment_returns_none_for_empty_or_bad_input(self):
        self.assertIsNone(_parse_moment(None))
        self.assertIsNone(_parse_moment(""))
        self.assertIsNone(_parse_moment("   "))
        self.assertIsNone(_parse_moment("not-a-date"))

    def test_parse_moment_assumes_utc_when_naive(self):
        parsed = _parse_moment("2026-10-11T00:30:00")
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_parse_moment_accepts_z_suffix(self):
        self.assertIsNotNone(_parse_moment("2026-10-11T00:30:00Z"))

    def test_window_defaults_to_the_last_seven_days(self):
        with patch("app.api.routes.portfolio.portfolio.utc_now") as now:
            now.return_value = utc_now().replace(microsecond=0)
            start, end = _equity_window(None, None, None)
        self.assertIsNone(end)
        self.assertAlmostEqual((now.return_value - start).total_seconds(), 7 * 86400, places=0)

    def test_window_date_selects_one_day(self):
        start, end = _equity_window(None, None, "2026-10-11")
        self.assertEqual(start.isoformat(), "2026-10-11T00:00:00+00:00")
        self.assertEqual((end - start).total_seconds(), 86400)

    def test_window_date_with_a_timestamp_selects_its_day(self):
        start, end = _equity_window(None, None, "2026-10-11T18:45:00Z")
        self.assertEqual(start.isoformat(), "2026-10-11T00:00:00+00:00")
        self.assertEqual((end - start).total_seconds(), 86400)

    def test_window_ignores_an_unparseable_date_and_uses_the_default(self):
        start, end = _equity_window(None, None, "garbage")
        self.assertIsNotNone(start)
        # ~7 days of look-back, no upper bound.
        self.assertIsNone(end)
        self.assertGreaterEqual(start.isoformat(), "2000-01-01")

    def test_window_passes_explicit_bounds_through(self):
        start, end = _equity_window("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", None)
        self.assertEqual(start.isoformat(), "2026-10-01T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-10-02T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
