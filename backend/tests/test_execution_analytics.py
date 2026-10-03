"""Execution analytics & Edge Score tests (plan 03).

Covers the slippage/latency math, the per-strategy
aggregation (fee drag, fill rate, win rate), Edge Score
normalization to 0–100, fill capture in the trade monitor,
and auth on the three /api/trades/analytics endpoints.

A real in-memory SQLite session is used (via SQLAlchemy)
so the ORM interaction and the aggregation queries are
genuinely exercised rather than mocked.
"""

import unittest
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.base import Base
from app.models.user import User
from app.models.user_trade import UserTrade
from app.services import trade_monitor
from app.services.execution_analytics import (
    STRATEGY_SOURCES,
    compute_latency_ms,
    compute_slippage_bps,
    get_edge_scores,
    get_execution_summary,
    get_strategy_stats,
    get_trade_details,
)
from app.utils.time import utc_now

WALLET = "0xexecution-analytics-wallet"
USER_ID = 7373

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class AnalyticsTestBase(unittest.TestCase):
    """Shared in-memory database with a user and the UserTrade table."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _trade(self, **kwargs):
        """Insert one UserTrade and return it."""
        defaults = {
            "user_id": USER_ID,
            "market_id": "0xmarket",
            "token_id": "0xtoken",
            "action": "buy",
            "amount": 50.0,
            "price": 0.5,
            "status": "executed",
            "created_at": utc_now(),
        }
        defaults.update(kwargs)
        trade = UserTrade(**defaults)
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        return trade


# ────────────── Slippage & latency math ──────────────


class SlippageMathTests(unittest.TestCase):
    """compute_slippage_bps: (filled − expected) / expected × 10⁴."""

    def test_positive_slippage(self):
        # 0.50 → 0.55 is +1000 bps (paid 10% more than quoted)
        self.assertEqual(compute_slippage_bps(0.5, 0.55), 1000.0)

    def test_negative_slippage(self):
        self.assertEqual(compute_slippage_bps(0.5, 0.45), -1000.0)

    def test_zero_slippage(self):
        self.assertEqual(compute_slippage_bps(0.5, 0.5), 0.0)

    def test_missing_expected_price_returns_none(self):
        self.assertIsNone(compute_slippage_bps(None, 0.5))

    def test_missing_filled_price_returns_none(self):
        self.assertIsNone(compute_slippage_bps(0.5, None))

    def test_zero_expected_price_returns_none(self):
        self.assertIsNone(compute_slippage_bps(0.0, 0.5))

    def test_negative_expected_price_returns_none(self):
        self.assertIsNone(compute_slippage_bps(-0.1, 0.5))

    def test_non_numeric_input_returns_none(self):
        self.assertIsNone(compute_slippage_bps("abc", 0.5))


class LatencyMathTests(unittest.TestCase):
    """compute_latency_ms: filled_at − submitted_at in milliseconds."""

    def test_latency_computation(self):
        submitted = BASE_TIME
        filled = BASE_TIME + timedelta(seconds=2, milliseconds=500)
        self.assertEqual(compute_latency_ms(submitted, filled), 2500.0)

    def test_missing_timestamps_return_none(self):
        now = utc_now()
        self.assertIsNone(compute_latency_ms(None, now))
        self.assertIsNone(compute_latency_ms(now, None))
        self.assertIsNone(compute_latency_ms(None, None))

    def test_naive_timestamp_treated_as_utc(self):
        # SQLite round-trips drop tzinfo; a naive value must
        # still compute against a tz-aware one.
        naive = BASE_TIME.replace(tzinfo=None)
        aware = BASE_TIME + timedelta(seconds=1)
        self.assertEqual(compute_latency_ms(naive, aware), 1000.0)


# ────────────── Aggregation: fee drag, fill rate, win rate ──────────────


class AggregationTests(AnalyticsTestBase):
    """Per-strategy rolling stats over a synthetic cohort."""

    def setUp(self):
        super().setUp()
        # copy: two filled (one win, one loss) + one pending
        self._trade(
            strategy_source="copy",
            status="executed",
            expected_price=0.5,
            expected_size=50.0,
            filled_price=0.55,
            filled_size=95.0,
            fee_paid=0.5,
            pnl=10.0,
        )
        self._trade(
            strategy_source="copy",
            status="executed",
            expected_price=0.5,
            expected_size=50.0,
            filled_price=0.48,
            filled_size=95.0,
            fee_paid=0.5,
            pnl=-5.0,
        )
        self._trade(
            strategy_source="copy",
            status="pending",
            expected_price=0.5,
            expected_size=50.0,
        )
        # manual: single all-win fill
        self._trade(
            strategy_source="manual",
            status="executed",
            expected_price=0.6,
            expected_size=25.0,
            filled_price=0.62,
            filled_size=24.0,
            fee_paid=0.25,
            pnl=4.0,
        )
        # legacy pre-migration trade: no strategy source
        self._trade(status="executed", pnl=100.0)

    def test_copy_strategy_stats(self):
        stats = get_strategy_stats(self.db, USER_ID)["copy"]
        self.assertEqual(stats.submissions, 3)
        self.assertEqual(stats.filled, 2)
        self.assertAlmostEqual(stats.fill_rate, 2 / 3, places=4)
        # slippage samples: +1000 bps and −400 bps
        self.assertAlmostEqual(stats.avg_slippage_bps, 300.0, places=2)
        self.assertAlmostEqual(stats.median_slippage_bps, 300.0, places=2)
        # fee drag: Σfees=1.0 vs Σgross realized PnL=5.0
        self.assertAlmostEqual(stats.total_fees, 1.0, places=6)
        self.assertAlmostEqual(stats.gross_realized_pnl, 5.0, places=6)
        self.assertAlmostEqual(stats.net_realized_pnl, 4.0, places=6)
        self.assertAlmostEqual(stats.fee_drag_ratio, 0.2, places=4)
        # win/loss
        self.assertEqual(stats.wins, 1)
        self.assertEqual(stats.losses, 1)
        self.assertAlmostEqual(stats.win_rate, 0.5, places=4)
        self.assertAlmostEqual(stats.avg_win, 10.0, places=6)
        self.assertAlmostEqual(stats.avg_loss, 5.0, places=6)
        self.assertAlmostEqual(stats.avg_fee, 0.5, places=6)
        self.assertAlmostEqual(stats.avg_risk_per_trade, 7.5, places=6)

    def test_manual_strategy_all_wins(self):
        stats = get_strategy_stats(self.db, USER_ID)["manual"]
        self.assertEqual(stats.submissions, 1)
        self.assertEqual(stats.filled, 1)
        self.assertEqual(stats.fill_rate, 1.0)
        self.assertAlmostEqual(stats.avg_slippage_bps, 333.33, places=2)
        # No losses: avg_loss defaults to 0.0, not None.
        self.assertEqual(stats.avg_loss, 0.0)
        self.assertAlmostEqual(stats.win_rate, 1.0, places=4)

    def test_legacy_trades_excluded_from_strategy_stats(self):
        stats = get_strategy_stats(self.db, USER_ID)
        self.assertNotIn("unknown", stats)
        self.assertEqual(set(stats), {"copy", "manual"})

    def test_summary_data_quality_counts_legacy(self):
        summary = get_execution_summary(self.db, USER_ID)
        self.assertEqual(summary["data_quality"]["legacy_trades"], 1)
        self.assertEqual(summary["data_quality"]["missing_expected_price"], 0)
        self.assertEqual(summary["data_quality"]["missing_fee"], 0)

    def test_summary_totals_aggregate_all_strategies(self):
        summary = get_execution_summary(self.db, USER_ID)
        totals = summary["totals"]
        self.assertEqual(totals["submissions"], 4)
        self.assertEqual(totals["filled"], 3)
        self.assertAlmostEqual(totals["fill_rate"], 0.75, places=4)
        self.assertAlmostEqual(totals["total_fees"], 1.25, places=6)
        self.assertAlmostEqual(totals["gross_realized_pnl"], 9.0, places=6)
        self.assertAlmostEqual(totals["net_realized_pnl"], 7.75, places=6)
        self.assertAlmostEqual(totals["wins"], 2)
        self.assertAlmostEqual(totals["losses"], 1)
        self.assertAlmostEqual(totals["win_rate"], 2 / 3, places=4)

    def test_missing_fee_treated_as_zero_and_counted(self):
        self._trade(
            strategy_source="copy",
            status="executed",
            expected_price=0.5,
            filled_price=0.5,
            fee_paid=None,
            pnl=1.0,
        )
        summary = get_execution_summary(self.db, USER_ID)
        self.assertEqual(summary["data_quality"]["missing_fee"], 1)
        copy_stats = get_strategy_stats(self.db, USER_ID)["copy"]
        # The missing fee contributes 0 to the total.
        self.assertAlmostEqual(copy_stats.total_fees, 1.0, places=6)
        self.assertEqual(copy_stats.missing_fee, 1)

    def test_window_excludes_old_trades(self):
        self._trade(
            strategy_source="copy",
            status="executed",
            expected_price=0.5,
            filled_price=0.5,
            fee_paid=0.1,
            pnl=1.0,
            created_at=utc_now() - timedelta(days=45),
        )
        stats = get_strategy_stats(self.db, USER_ID, window_days=30)
        self.assertEqual(stats["copy"].submissions, 3)
        wider = get_strategy_stats(self.db, USER_ID, window_days=90)
        self.assertEqual(wider["copy"].submissions, 4)

    def test_trade_details_strategy_filter_and_limit(self):
        rows = get_trade_details(self.db, USER_ID, strategy="manual")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy_source"], "manual")
        all_rows = get_trade_details(self.db, USER_ID, limit=200)
        self.assertEqual(len(all_rows), 5)
        # Newest first: the legacy trade was inserted last.
        self.assertIsNone(all_rows[0]["strategy_source"])
        sources = {r["strategy_source"] for r in all_rows}
        self.assertEqual(sources, {None, "copy", "manual"})


# ────────────── Edge Score normalization ──────────────


class EdgeScoreTests(AnalyticsTestBase):
    """Edge Score = 50 + 50·tanh(expectancy / avg_risk), in (0, 100)."""

    def _filled(self, strategy, pnl, fee=0.1, expected=0.5, filled=0.5):
        self._trade(
            strategy_source=strategy,
            status="executed",
            expected_price=expected,
            expected_size=50.0,
            filled_price=filled,
            filled_size=95.0,
            fee_paid=fee,
            pnl=pnl,
        )

    def test_edge_score_bounded_and_centered(self):
        from app.services.execution_analytics import _normalize_edge_score

        self.assertAlmostEqual(_normalize_edge_score(0.0), 50.0, places=6)
        self.assertGreater(_normalize_edge_score(1.0), 50.0)
        self.assertLess(_normalize_edge_score(1.0), 100.0)
        self.assertLess(_normalize_edge_score(-1.0), 50.0)
        self.assertGreater(_normalize_edge_score(-1.0), 0.0)
        # tanh saturates toward but never crosses the bounds.
        self.assertLessEqual(_normalize_edge_score(100.0), 100.0)
        self.assertGreaterEqual(_normalize_edge_score(-100.0), 0.0)
        self.assertLess(_normalize_edge_score(10.0), 100.0)
        self.assertGreater(_normalize_edge_score(-10.0), 0.0)

    def test_winning_strategy_scores_above_break_even(self):
        for _ in range(3):
            self._filled("copy", pnl=10.0, fee=0.1)
        rows = get_edge_scores(self.db, USER_ID)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy"], "copy")
        self.assertIsNotNone(row["edge_score"])
        self.assertGreater(row["edge_score"], 50.0)
        self.assertLess(row["edge_score"], 100.0)
        self.assertAlmostEqual(row["win_rate"], 1.0, places=4)

    def test_losing_strategy_scores_below_break_even(self):
        for _ in range(3):
            self._filled("copy", pnl=-10.0, fee=0.1)
        row = get_edge_scores(self.db, USER_ID)[0]
        self.assertIsNotNone(row["edge_score"])
        self.assertLess(row["edge_score"], 50.0)
        self.assertGreater(row["edge_score"], 0.0)

    def test_break_even_strategy_scores_near_fifty(self):
        # Equal wins/losses with zero fees: expectancy ≈ 0.
        self._filled("copy", pnl=10.0, fee=0.0)
        self._filled("copy", pnl=-10.0, fee=0.0)
        row = get_edge_scores(self.db, USER_ID)[0]
        self.assertIsNotNone(row["edge_score"])
        self.assertAlmostEqual(row["edge_score"], 50.0, delta=1.0)

    def test_unfilled_strategy_has_null_edge_score(self):
        self._trade(strategy_source="copy", status="pending")
        row = get_edge_scores(self.db, USER_ID)[0]
        self.assertIsNone(row["edge_score"])
        self.assertEqual(row["closed_trades"], 0)

    def test_edge_scores_sorted_best_first(self):
        self._filled("copy", pnl=10.0, fee=0.1)
        self._filled("manual", pnl=-10.0, fee=0.1)
        rows = get_edge_scores(self.db, USER_ID)
        scores = [r["edge_score"] for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_known_edge_score_value(self):
        # One win (+10) and one loss (−5), fee 0.5 each:
        # win_rate=0.5, avg_win=10, avg_loss=5, avg_fee=0.5,
        # avg_risk=7.5 → expectancy=2.0 → edge_raw=0.2667
        self._filled("copy", pnl=10.0, fee=0.5)
        self._filled("copy", pnl=-5.0, fee=0.5)
        row = get_edge_scores(self.db, USER_ID)[0]
        self.assertAlmostEqual(row["edge_raw"], 2.0 / 7.5, places=3)
        self.assertAlmostEqual(row["edge_score"], 63.03, delta=0.5)


# ────────────── Fill capture (trade_monitor) ──────────────


class FillCaptureTests(AnalyticsTestBase):
    """An observed fill records filled_*, fee, slippage and latency."""

    def _pending(self, **kwargs):
        defaults = {
            "strategy_source": "copy",
            "status": "pending",
            "order_hash": "0xfill-hash",
            "expected_price": 0.5,
            "expected_size": 50.0,
            "created_at": utc_now() - timedelta(seconds=2),
        }
        defaults.update(kwargs)
        return self._trade(**defaults)

    def test_fill_records_price_size_fee_slippage_latency(self):
        trade = self._pending()
        filled_at = utc_now()
        marked = trade_monitor._mark_pending_trades_filled(
            self.db,
            "0xfill-hash",
            filled_at=filled_at,
            fill_price=0.55,
            fill_size=95.0,
            fill_fee=0.42,
        )
        self.assertEqual(marked, 1)
        self.db.refresh(trade)
        self.assertEqual(trade.status, "executed")
        self.assertEqual(trade.filled_price, 0.55)
        self.assertEqual(trade.filled_size, 95.0)
        self.assertAlmostEqual(trade.fee_paid, 0.42, places=6)
        self.assertAlmostEqual(trade.slippage_bps, 1000.0, places=2)
        self.assertIsNotNone(trade.latency_ms)
        self.assertGreaterEqual(trade.latency_ms, 0.0)

    def test_fill_without_fee_defaults_to_zero(self):
        trade = self._pending()
        trade_monitor._mark_pending_trades_filled(
            self.db,
            "0xfill-hash",
            filled_at=utc_now(),
            fill_price=0.5,
        )
        self.db.refresh(trade)
        self.assertAlmostEqual(trade.fee_paid, 0.0, places=6)
        self.assertAlmostEqual(trade.slippage_bps, 0.0, places=2)

    def test_fill_without_price_leaves_slippage_null(self):
        trade = self._pending()
        trade_monitor._mark_pending_trades_filled(
            self.db,
            "0xfill-hash",
            filled_at=utc_now(),
        )
        self.db.refresh(trade)
        self.assertIsNone(trade.filled_price)
        self.assertIsNone(trade.slippage_bps)
        self.assertAlmostEqual(trade.fee_paid, 0.0, places=6)

    def test_fill_without_expected_price_leaves_slippage_null(self):
        trade = self._pending(expected_price=None)
        trade_monitor._mark_pending_trades_filled(
            self.db,
            "0xfill-hash",
            filled_at=utc_now(),
            fill_price=0.55,
        )
        self.db.refresh(trade)
        self.assertEqual(trade.filled_price, 0.55)
        self.assertIsNone(trade.slippage_bps)

    def test_fill_ignores_other_order_hashes(self):
        self._pending(order_hash="0xother")
        marked = trade_monitor._mark_pending_trades_filled(
            self.db,
            "0xnonexistent",
            filled_at=utc_now(),
            fill_price=0.55,
        )
        self.assertEqual(marked, 0)


# ────────────── Endpoint auth ──────────────


class EndpointAuthTests(unittest.TestCase):
    """The analytics endpoints require authentication."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app), app

    def _override(self, app):
        from app.api.routes.auth import get_current_user_from_token
        from app.utils.database import get_db

        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": USER_ID,
            "wallet_address": WALLET,
            "is_admin": False,
        }

        def _override_get_db():
            yield self.db

        app.dependency_overrides[get_db] = _override_get_db

    def test_summary_requires_auth(self):
        client, _ = self._client()
        resp = client.get("/api/trades/analytics/summary")
        self.assertEqual(resp.status_code, 401)

    def test_edge_score_requires_auth(self):
        client, _ = self._client()
        resp = client.get("/api/trades/analytics/edge-score")
        self.assertEqual(resp.status_code, 401)

    def test_trade_details_requires_auth(self):
        client, _ = self._client()
        resp = client.get("/api/trades/analytics/trades")
        self.assertEqual(resp.status_code, 401)

    def test_summary_returns_valid_schema_when_authenticated(self):
        client, app = self._client()
        self._override(app)
        try:
            resp = client.get("/api/trades/analytics/summary", params={"window_days": 30})
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user_id"], USER_ID)
        self.assertEqual(body["window_days"], 30)
        self.assertIn("generated_at", body)
        self.assertIn("per_strategy", body)
        self.assertIn("totals", body)
        self.assertIn("data_quality", body)
        for key in ("legacy_trades", "missing_expected_price", "missing_fee"):
            self.assertIn(key, body["data_quality"])
        for key in (
            "strategy",
            "submissions",
            "filled",
            "fill_rate",
            "avg_slippage_bps",
            "median_slippage_bps",
            "total_fees",
            "gross_realized_pnl",
            "net_realized_pnl",
            "fee_drag_ratio",
            "win_rate",
            "avg_win",
            "avg_loss",
            "avg_fee",
            "avg_risk_per_trade",
            "edge_raw",
            "edge_score",
        ):
            self.assertIn(key, body["totals"])

    def test_edge_score_returns_valid_schema_when_authenticated(self):
        client, app = self._client()
        self._override(app)
        try:
            resp = client.get("/api/trades/analytics/edge-score")
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user_id"], USER_ID)
        self.assertIn("strategies", body)
        self.assertIsInstance(body["strategies"], list)

    def test_trade_details_returns_valid_schema_when_authenticated(self):
        client, app = self._client()
        self._override(app)
        try:
            resp = client.get(
                "/api/trades/analytics/trades",
                params={"strategy": "copy", "limit": 10},
            )
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["user_id"], USER_ID)
        self.assertEqual(body["strategy"], "copy")
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["trades"], [])

    def test_trade_details_rejects_unknown_strategy(self):
        client, app = self._client()
        self._override(app)
        try:
            resp = client.get("/api/trades/analytics/trades", params={"strategy": "nope"})
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 400)

    def test_strategy_sources_enum_is_stable(self):
        self.assertEqual(
            STRATEGY_SOURCES,
            (
                "copy",
                "manual",
                "market_maker",
                "inverse",
                "stop_loss",
                "take_profit",
                "latency_arb",
            ),
        )


if __name__ == "__main__":
    unittest.main()
