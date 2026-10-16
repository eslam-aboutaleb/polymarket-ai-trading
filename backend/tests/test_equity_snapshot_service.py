"""Tests for app/services/equity_snapshot_service.py (plan 05).

The wallet is faked at the ``PolymarketService`` boundary, so the real
``kelly_service.compute_bankroll`` runs end to end; every write and read
runs against real in-memory SQLite (the ``test_fill_tracking.py``
pattern).
"""

import asyncio
import contextlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.analytics.assessment import Assessment
from app.models.analytics.portfolio_snapshot import PortfolioSnapshot
from app.models.base import Base
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.services.analytics import equity_snapshot_service as svc
from app.utils.time import utc_now

WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20

BACKEND_DIR = Path(__file__).resolve().parents[1]


class FakePolymarketService:
    """Minimal ``PolymarketService`` double with injectable failures."""

    def __init__(
        self,
        usdc_balance=100.0,
        positions=None,
        balance_error=None,
        positions_error=None,
    ):
        self.usdc_balance = usdc_balance
        self.positions = positions if positions is not None else []
        self.balance_error = balance_error
        self.positions_error = positions_error
        self.balance_calls = 0
        self.position_calls = 0

    async def get_wallet_balance(self, wallet_address, private_key=None, clob_creds=None):
        self.balance_calls += 1
        if self.balance_error:
            raise self.balance_error
        return {"wallet_address": wallet_address, "usdc_balance": self.usdc_balance}

    async def get_positions(self, wallet_address, private_key=None, clob_creds=None):
        self.position_calls += 1
        if self.positions_error:
            raise self.positions_error
        return self.positions


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _user(self, user_id=1, wallet=WALLET):
        user = User(id=user_id, wallet_address=wallet)
        self.db.add(user)
        self.db.commit()
        return user

    def _settings(self, user_id=1, **kwargs):
        row = UserSettings(user_id=user_id, **kwargs)
        self.db.add(row)
        self.db.commit()
        return row

    def _all_rows(self, user_id=None):
        query = self.db.query(PortfolioSnapshot)
        if user_id is not None:
            query = query.filter(PortfolioSnapshot.user_id == user_id)
        return query.order_by(PortfolioSnapshot.bucket_hour).all()

    def _patch_service(self, service):
        # The service imports the factory at module level, so the reference to
        # patch is the one bound in its own namespace.
        return patch.object(svc, "get_polymarket_service", return_value=service)

    def _patch_credentials(self, stored=None):
        return patch.object(svc, "load_wallet_credentials", return_value=stored)


class HelperTests(unittest.TestCase):
    def test_truncate_to_hour_drops_minutes_and_below(self):
        moment = utc_now().replace(minute=37, second=41, microsecond=999)
        bucket = svc.truncate_to_hour(moment)
        self.assertEqual(
            (bucket.minute, bucket.second, bucket.microsecond, bucket.hour),
            (0, 0, 0, moment.hour),
        )

    def test_period_window_maps_known_labels(self):
        self.assertEqual(svc.period_window("24h"), timedelta(hours=24))
        self.assertEqual(svc.period_window("7d"), timedelta(days=7))
        self.assertIsNone(svc.period_window("all"))
        # Unknown / empty falls back to the 7-day default rather than raising.
        self.assertEqual(svc.period_window("nonsense"), timedelta(days=7))
        self.assertEqual(svc.period_window(None), timedelta(days=7))

    def test_position_totals_accepts_both_spellings(self):
        positions = [
            {"size": 10, "avgPrice": 0.4, "curPrice": 0.6},
            {"size": 5, "avg_price": 0.2, "cur_price": 0.1},
            # No live price: falls back to the entry price.
            {"size": 2, "avgPrice": 0.5},
            # Not a position.
            {"size": 0, "avgPrice": 1.0, "curPrice": 1.0},
            {"not": "a dict"},
        ]
        totals = svc.position_totals(positions)
        self.assertEqual(totals["positions_count"], 3)
        self.assertAlmostEqual(totals["total_invested"], 10 * 0.4 + 5 * 0.2 + 2 * 0.5)
        self.assertAlmostEqual(totals["total_current_value"], 10 * 0.6 + 5 * 0.1 + 2 * 0.5)

    def test_position_totals_tolerates_none_and_empty(self):
        self.assertEqual(
            svc.position_totals(None),
            {"total_invested": 0.0, "total_current_value": 0.0, "positions_count": 0},
        )

    def test_max_drawdown_pct(self):
        # Peak 100 → trough 50 = -50%.
        self.assertAlmostEqual(svc.max_drawdown_pct([100.0, 80.0, 50.0, 90.0]), 50.0)
        # A rising series never draws down.
        self.assertEqual(svc.max_drawdown_pct([10.0, 20.0, 30.0]), 0.0)
        self.assertEqual(svc.max_drawdown_pct([]), 0.0)


class CaptureUserSnapshotTests(ServiceTestCase):
    async def test_live_user_writes_one_row_with_kelly_bankroll(self):
        user = self._user()
        service = FakePolymarketService(
            usdc_balance=250.0,
            positions=[{"size": 10, "avgPrice": 0.4, "curPrice": 0.6}],
        )
        with self._patch_service(service), self._patch_credentials({"private_key": "0xpk"}):
            snapshot = await svc.capture_user_snapshot(self.db, user)

        self.assertIsNotNone(snapshot)
        self.assertFalse(snapshot.is_paper)
        self.assertEqual(snapshot.usdc_balance, 250.0)
        # bankroll = 250 USDC + 10*(0.6-0.4) = 252, straight from compute_bankroll.
        self.assertEqual(snapshot.bankroll, 252.0)
        self.assertEqual(snapshot.unrealized_pnl, 2.0)
        self.assertEqual(snapshot.positions_count, 1)
        self.assertEqual(snapshot.total_invested, 4.0)
        self.assertEqual(snapshot.total_current_value, 6.0)

        rows = self._all_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].bucket_hour, svc.truncate_to_hour(rows[0].bucket_hour))
        # One wallet fetch pair per snapshot — no duplicate upstream calls.
        self.assertEqual(service.balance_calls, 1)
        self.assertEqual(service.position_calls, 1)

    async def test_second_call_in_same_hour_writes_nothing(self):
        user = self._user()
        service = FakePolymarketService(usdc_balance=100.0)
        with self._patch_service(service), self._patch_credentials(None):
            first = await svc.capture_user_snapshot(self.db, user)
            second = await svc.capture_user_snapshot(self.db, user)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self._all_rows()), 1)
        # The idempotency check short-circuits before any upstream call.
        self.assertEqual(service.balance_calls, 1)

    async def test_paper_user_tracks_paper_balance_not_the_wallet(self):
        user = self._user()
        self._settings(user.id, simulation_mode=True, paper_balance=1375.5)
        service = FakePolymarketService(usdc_balance=9999.0)
        with self._patch_service(service), self._patch_credentials(None):
            snapshot = await svc.capture_user_snapshot(self.db, user)

        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.is_paper)
        self.assertEqual(snapshot.bankroll, 1375.5)
        # Wallet columns describe the real wallet, so they stay zero.
        self.assertEqual(snapshot.usdc_balance, 0.0)
        self.assertEqual(snapshot.unrealized_pnl, 0.0)
        self.assertEqual(snapshot.positions_count, 0)
        # A paper account needs no wallet calls at all.
        self.assertEqual(service.balance_calls, 0)
        self.assertEqual(service.position_calls, 0)

    async def test_paper_and_live_users_get_separate_series(self):
        live_user = self._user(user_id=1, wallet=WALLET)
        self._settings(1)  # simulation_mode defaults to False
        paper_user = self._user(user_id=2, wallet=OTHER_WALLET)
        self._settings(2, simulation_mode=True, paper_balance=500.0)
        service = FakePolymarketService(usdc_balance=100.0)
        with self._patch_service(service), self._patch_credentials(None):
            live = await svc.capture_user_snapshot(self.db, live_user)
            paper = await svc.capture_user_snapshot(self.db, paper_user)

        self.assertFalse(live.is_paper)
        self.assertTrue(paper.is_paper)
        self.assertEqual(len(self._all_rows()), 2)

    async def test_balance_failure_skips_the_user_with_no_row(self):
        user = self._user()
        service = FakePolymarketService(
            usdc_balance=100.0,
            balance_error=RuntimeError("clob down"),
            positions=[{"size": 1, "avgPrice": 0.5, "curPrice": 0.6}],
        )
        with self._patch_service(service), self._patch_credentials(None):
            snapshot = await svc.capture_user_snapshot(self.db, user)

        self.assertIsNone(snapshot)
        # A failed fetch must never be recorded as a zero equity point.
        self.assertEqual(self._all_rows(), [])
        self.assertEqual(service.balance_calls, 1)

    async def test_positions_failure_skips_the_user_with_no_row(self):
        user = self._user()
        service = FakePolymarketService(usdc_balance=100.0, positions_error=OSError("timeout"))
        with self._patch_service(service), self._patch_credentials(None):
            snapshot = await svc.capture_user_snapshot(self.db, user)

        self.assertIsNone(snapshot)
        self.assertEqual(self._all_rows(), [])

    async def test_credential_store_error_is_survivable(self):
        user = self._user()
        service = FakePolymarketService(usdc_balance=42.0)
        with (
            self._patch_service(service),
            patch.object(
                svc,
                "load_wallet_credentials",
                side_effect=svc.CredentialStoreError("encryption not configured"),
            ),
        ):
            snapshot = await svc.capture_user_snapshot(self.db, user)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.bankroll, 42.0)

    async def test_compute_bankroll_raising_is_survivable(self):
        """compute_bankroll promises never to raise; if it ever does we skip."""
        user = self._user()
        service = FakePolymarketService(usdc_balance=42.0)
        with (
            self._patch_service(service),
            self._patch_credentials(None),
            patch.object(
                svc.kelly_service,
                "compute_bankroll",
                side_effect=RuntimeError("boom"),
            ),
        ):
            snapshot = await svc.capture_user_snapshot(self.db, user)

        self.assertIsNone(snapshot)
        self.assertEqual(self._all_rows(), [])

    async def test_user_without_settings_row_is_live(self):
        user = self._user()
        service = FakePolymarketService(usdc_balance=10.0)
        with self._patch_service(service), self._patch_credentials(None):
            snapshot = await svc.capture_user_snapshot(self.db, user)
        self.assertFalse(snapshot.is_paper)


class RunSnapshotCycleTests(ServiceTestCase):
    async def test_one_row_per_user(self):
        self._user(user_id=1, wallet=WALLET)
        self._user(user_id=2, wallet=OTHER_WALLET)
        self._settings(2, simulation_mode=True, paper_balance=321.0)
        service = FakePolymarketService(usdc_balance=100.0)
        with self._patch_service(service), self._patch_credentials(None):
            result = await svc.run_snapshot_cycle(self.db)

        self.assertEqual(result, {"users": 2, "written": 2, "skipped": 0})
        self.assertEqual(len(self._all_rows()), 2)

    async def test_a_failing_user_does_not_abort_the_cycle(self):
        self._user(user_id=1, wallet=WALLET)
        self._user(user_id=2, wallet=OTHER_WALLET)
        # User 2's fetch blows up in a way capture does not catch.
        service = FakePolymarketService(usdc_balance=100.0)
        real_get_positions = service.get_positions

        async def _boom(wallet_address, private_key=None, clob_creds=None):
            if wallet_address == OTHER_WALLET:
                raise MemoryError("unexpected late failure")
            return await real_get_positions(wallet_address, private_key, clob_creds)

        service.get_positions = _boom
        with self._patch_service(service), self._patch_credentials(None):
            result = await svc.run_snapshot_cycle(self.db)

        self.assertEqual(result["written"], 1)
        self.assertGreaterEqual(result["skipped"], 1)
        rows = self._all_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_id, 1)

    async def test_users_are_sampled_concurrently(self):
        """One slow user must not serialise the whole pass."""
        for user_id in range(1, 5):
            self._user(user_id=user_id, wallet=f"0x{user_id:040x}")
            self._settings(user_id)
        release = asyncio.Event()
        started = 0

        class _GatedService(FakePolymarketService):
            """Every balance call blocks until the release event is set."""

            async def get_wallet_balance(self, wallet_address, private_key=None, clob_creds=None):
                nonlocal started
                started += 1
                await release.wait()
                return await super().get_wallet_balance(
                    wallet_address, private_key=private_key, clob_creds=clob_creds
                )

        service = _GatedService(usdc_balance=100.0)

        async def _run_cycle():
            with self._patch_service(service), self._patch_credentials(None):
                return await svc.run_snapshot_cycle(self.db)

        cycle = asyncio.create_task(_run_cycle())
        await asyncio.sleep(0.05)
        # Every user is inside the gated call: a sequential implementation
        # would still be waiting on user 1's balance.
        self.assertEqual(started, 4)
        self.assertFalse(cycle.done())
        release.set()
        result = await cycle

        self.assertEqual(result, {"users": 4, "written": 4, "skipped": 0})
        self.assertEqual(len(self._all_rows()), 4)

    async def test_concurrency_is_bounded(self):
        """At most MAX_CONCURRENT_SNAPSHOTS users are in flight at once."""
        total = 24
        for user_id in range(1, total + 1):
            self._user(user_id=user_id, wallet=f"0x{user_id:040x}")
            self._settings(user_id)

        state = {"in_flight": 0, "peak": 0}

        class _CountingService(FakePolymarketService):
            async def get_wallet_balance(self, wallet_address, private_key=None, clob_creds=None):
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
                try:
                    await asyncio.sleep(0)
                    return await super().get_wallet_balance(
                        wallet_address, private_key=private_key, clob_creds=clob_creds
                    )
                finally:
                    state["in_flight"] -= 1

        with (
            self._patch_service(_CountingService(usdc_balance=10.0)),
            self._patch_credentials(None),
        ):
            result = await svc.run_snapshot_cycle(self.db)

        self.assertEqual(result["written"], total)
        self.assertLessEqual(state["peak"], svc.MAX_CONCURRENT_SNAPSHOTS)
        # …and the semaphore really did open more than one lane.
        self.assertGreater(state["peak"], 1)

    async def test_a_hung_user_times_out_without_zeroing_the_others(self):
        for user_id in range(1, 5):
            self._user(user_id=user_id, wallet=f"0x{user_id:040x}")
            self._settings(user_id)

        class _HungService(FakePolymarketService):
            """User 3's balance call never returns."""

            async def get_wallet_balance(self, wallet_address, private_key=None, clob_creds=None):
                if wallet_address == f"0x{3:040x}":
                    await asyncio.Event().wait()
                return await super().get_wallet_balance(
                    wallet_address, private_key=private_key, clob_creds=clob_creds
                )

        with (
            patch.object(svc, "SNAPSHOT_USER_TIMEOUT_SECONDS", 0.05),
            self._patch_service(_HungService(usdc_balance=42.0)),
            self._patch_credentials(None),
        ):
            result = await asyncio.wait_for(svc.run_snapshot_cycle(self.db), timeout=10)

        self.assertEqual(result["users"], 4)
        self.assertEqual(result["written"], 3)
        self.assertEqual(result["skipped"], 1)
        rows = self._all_rows()
        # The timed-out user is skipped, not recorded as a zero equity point.
        self.assertEqual(len(rows), 3)
        self.assertNotIn(3, {row.user_id for row in rows})
        self.assertTrue(all(row.bankroll == 42.0 for row in rows))

    async def test_no_users_is_a_no_op(self):
        result = await svc.run_snapshot_cycle(self.db)
        self.assertEqual(result, {"users": 0, "written": 0, "skipped": 0})


class ReadPathTests(ServiceTestCase):
    def _seed(self, user_id=1, bankrolls=(), paper=False, hours_ago=()):
        now = utc_now()
        for bankroll, age in zip(bankrolls, hours_ago, strict=False):
            captured = now - timedelta(hours=age)
            self.db.add(
                PortfolioSnapshot(
                    user_id=user_id,
                    bucket_hour=svc.truncate_to_hour(captured),
                    captured_at=captured,
                    usdc_balance=bankroll - (2.0 if not paper else 0.0),
                    unrealized_pnl=0.0 if paper else 2.0,
                    bankroll=bankroll,
                    total_invested=0.0,
                    total_current_value=0.0,
                    positions_count=0,
                    is_paper=paper,
                )
            )
        self.db.commit()

    def test_summary_math_including_max_drawdown(self):
        # 100 → 80 → 60 → 120: peak-to-trough 100→60 = -40%.
        self._seed(bankrolls=[100.0, 80.0, 60.0, 120.0], hours_ago=[10, 8, 6, 4])
        summary = svc.summarize_snapshots(self.db, 1, "7d")
        self.assertEqual(summary["period"], "7d")
        self.assertEqual(summary["start"], 100.0)
        self.assertEqual(summary["end"], 120.0)
        self.assertEqual(summary["delta"], 20.0)
        self.assertEqual(summary["delta_pct"], 20.0)
        self.assertAlmostEqual(summary["max_drawdown_pct"], 40.0)
        self.assertEqual(summary["points"], 4)

    def test_summary_empty_history_is_zeros_not_an_error(self):
        summary = svc.summarize_snapshots(self.db, 999, "24h")
        self.assertEqual(summary["points"], 0)
        self.assertEqual(summary["start"], 0.0)
        self.assertEqual(summary["end"], 0.0)
        self.assertEqual(summary["delta"], 0.0)
        self.assertEqual(summary["delta_pct"], 0.0)
        self.assertEqual(summary["max_drawdown_pct"], 0.0)

    def test_summary_period_window_excludes_older_points(self):
        self._seed(bankrolls=[100.0, 200.0], hours_ago=[72, 1])
        summary = svc.summarize_snapshots(self.db, 1, "24h")
        self.assertEqual(summary["points"], 1)
        self.assertEqual(summary["start"], 200.0)
        self.assertEqual(summary["end"], 200.0)

    def test_summary_all_time_keeps_every_point(self):
        self._seed(bankrolls=[100.0, 200.0], hours_ago=[500, 1])
        summary = svc.summarize_snapshots(self.db, 1, "all")
        self.assertEqual(summary["points"], 2)
        self.assertEqual(summary["delta"], 100.0)

    def test_summary_zero_start_avoids_dividing_by_zero(self):
        self._seed(bankrolls=[0.0, 0.0], hours_ago=[2, 1])
        summary = svc.summarize_snapshots(self.db, 1, "7d")
        self.assertEqual(summary["delta_pct"], 0.0)

    def test_summary_scopes_to_one_user(self):
        self._seed(user_id=1, bankrolls=[100.0], hours_ago=[1])
        self._seed(user_id=2, bankrolls=[900.0], hours_ago=[1])
        summary = svc.summarize_snapshots(self.db, 1, "7d")
        self.assertEqual(summary["points"], 1)
        self.assertEqual(summary["end"], 100.0)

    def test_summary_never_spans_two_accounts(self):
        # A user who flips simulation_mode leaves paper points in the early
        # hours and live points in the later ones; an unfiltered summary then
        # reports a delta between two different bankrolls.
        self._seed(bankrolls=[1000.0, 100.0], hours_ago=[4, 2])
        self._seed(bankrolls=[500.0, 600.0], hours_ago=[3, 1], paper=True)

        live = svc.summarize_snapshots(self.db, 1, "all", is_paper=False)
        paper = svc.summarize_snapshots(self.db, 1, "all", is_paper=True)
        both = svc.summarize_snapshots(self.db, 1, "all")

        self.assertEqual((live["points"], live["start"], live["end"]), (2, 1000.0, 100.0))
        self.assertEqual((paper["points"], paper["start"], paper["end"]), (2, 500.0, 600.0))
        # Unfiltered still blends them: 1000 → 600, a delta of neither account.
        self.assertEqual(both["points"], 4)
        self.assertEqual(both["delta"], -400.0)

    def test_summary_account_filter_keeps_the_drawdown_inside_one_account(self):
        self._seed(bankrolls=[100.0, 50.0], hours_ago=[5, 4])
        self._seed(bankrolls=[900.0, 10.0], hours_ago=[3, 2], paper=True)

        live = svc.summarize_snapshots(self.db, 1, "7d", is_paper=False)

        # 100 → 50 = -50%; the paper series' -98.9% must not reach the number.
        self.assertAlmostEqual(live["max_drawdown_pct"], 50.0)

    def test_get_snapshots_returns_ascending_and_scoped(self):
        self._seed(bankrolls=[100.0, 110.0, 120.0], hours_ago=[3, 2, 1])
        self._seed(user_id=2, bankrolls=[999.0], hours_ago=[1])
        rows = svc.get_snapshots(self.db, 1)
        self.assertEqual([row.bankroll for row in rows], [100.0, 110.0, 120.0])
        self.assertTrue(all(row.user_id == 1 for row in rows))

    def test_get_snapshots_limit_keeps_the_newest_points_in_order(self):
        self._seed(bankrolls=[100.0, 110.0, 120.0], hours_ago=[3, 2, 1])
        rows = svc.get_snapshots(self.db, 1, limit=2)
        self.assertEqual([row.bankroll for row in rows], [110.0, 120.0])

    def test_get_snapshots_limit_is_clamped(self):
        self._seed(bankrolls=[100.0], hours_ago=[1])
        self.assertEqual(len(svc.get_snapshots(self.db, 1, limit=0)), 1)
        self.assertEqual(len(svc.get_snapshots(self.db, 1, limit=-5)), 1)

    def test_get_snapshots_window_filters_by_bucket(self):
        self._seed(bankrolls=[100.0, 110.0], hours_ago=[48, 2])
        now = utc_now()
        rows = svc.get_snapshots(self.db, 1, start=now - timedelta(hours=24))
        self.assertEqual([row.bankroll for row in rows], [110.0])

    def test_get_snapshots_filters_to_one_account(self):
        # paper=True / paper=False selects one bankroll; the unfiltered default
        # still returns both, so a caller that pairs rows itself can.
        self._seed(bankrolls=[900.0], hours_ago=[3], paper=True)
        self._seed(bankrolls=[100.0], hours_ago=[2])
        self._seed(bankrolls=[110.0], hours_ago=[1])

        live = svc.get_snapshots(self.db, 1, is_paper=False)
        paper = svc.get_snapshots(self.db, 1, is_paper=True)
        both = svc.get_snapshots(self.db, 1)

        self.assertEqual([row.bankroll for row in live], [100.0, 110.0])
        self.assertEqual([row.bankroll for row in paper], [900.0])
        self.assertEqual(len(both), 3)

    def test_get_snapshots_account_filter_composes_with_the_window(self):
        self._seed(bankrolls=[900.0], hours_ago=[3], paper=True)
        self._seed(bankrolls=[100.0], hours_ago=[48], paper=False)
        self._seed(bankrolls=[110.0], hours_ago=[2], paper=False)
        now = utc_now()

        rows = svc.get_snapshots(self.db, 1, start=now - timedelta(hours=24), is_paper=False)

        self.assertEqual([row.bankroll for row in rows], [110.0])

    def test_serialize_snapshot_wire_shape(self):
        self._seed(bankrolls=[100.0], hours_ago=[1], paper=True)
        row = self._all_rows()[0]
        payload = svc.serialize_snapshot(row)
        self.assertEqual(
            sorted(payload),
            [
                "bankroll",
                "bucket_hour",
                "captured_at",
                "is_paper",
                "positions_count",
                "total_current_value",
                "total_invested",
                "unrealized_pnl",
                "usdc_balance",
            ],
        )
        self.assertTrue(payload["is_paper"])
        self.assertEqual(payload["bankroll"], 100.0)


class AlembicChainTests(unittest.TestCase):
    """The alembic chain that creates this service's table.

    Real ``alembic`` runs on a scratch SQLite file, not an in-memory
    database: the point is the DDL a *deployed* database receives, so the
    same path the operator runs is the path under test.

    They live beside the snapshot service because 20261011_0010 creates
    ``portfolio_snapshots`` and 20261011_0009 is the revision that has to
    downgrade cleanly for the chain to be re-runnable at all.
    """

    def _run(self, db_path, *args):
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": ".",
                "DATABASE_URL": f"sqlite:///{db_path}",
                "JWT_SECRET_KEY": "a" * 32,
                "REFRESH_TOKEN_HASH_SECRET": "b" * 32,
                "ENVIRONMENT": "test",
            }
        )
        return subprocess.run(  # noqa: S603 — fixed argv: sys.executable + alembic
            [sys.executable, "-m", "alembic", *args],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _schema(self, db_path, table):
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                    (table,),
                )
            }
            # (referenced table, from column, to column)
            query = f"PRAGMA foreign_key_list({table})"
            foreign_keys = [(row[2], row[3], row[4]) for row in conn.execute(query)]
            return tables, indexes, foreign_keys
        finally:
            conn.close()

    def test_snapshots_migration_matches_the_model(self):
        """A migrated DB and a ``create_all`` DB must be the same table.

        Migration 0010's own ``create_table`` is exercised by dropping the
        table the chain just built: on a database where the table is absent
        at revision 0010 it is this DDL — not the model — that defines the
        schema, so it has to carry the FK and both ``index=True`` columns.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "migrated.db"
            staged = self._run(db_path, "upgrade", "20261011_0009")
            self.assertEqual(staged.returncode, 0, msg=f"staging failed:\n{staged.stderr}")

            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DROP TABLE portfolio_snapshots")
                conn.commit()
            finally:
                conn.close()

            result = self._run(db_path, "upgrade", "head")
            self.assertEqual(
                result.returncode, 0, msg=f"upgrade head failed:\n{result.stdout}\n{result.stderr}"
            )

            tables, indexes, foreign_keys = self._schema(db_path, "portfolio_snapshots")
            self.assertIn("portfolio_snapshots", tables)
            # Model parity: the user_id FK and both index=True columns.
            self.assertIn(("users", "user_id", "id"), foreign_keys)
            self.assertIn("ix_portfolio_snapshots_user_id", indexes)
            self.assertIn("ix_portfolio_snapshots_bucket_hour", indexes)
            self.assertIn("uq_portfolio_snapshots_user_bucket", indexes)

    def test_calibration_downgrade_is_deterministic(self):
        """Scan verdicts (``trade_history_id IS NULL``) cannot block a rollback.

        The 0009 downgrade re-imposes NOT NULL on that column, so it must
        first delete the rows that can never satisfy it — otherwise the batch
        ALTER raises IntegrityError and leaves ``_alembic_tmp_assessments``
        behind on SQLite.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "calibrated.db"
            upgrade = self._run(db_path, "upgrade", "head")
            self.assertEqual(upgrade.returncode, 0, msg=f"upgrade head failed:\n{upgrade.stderr}")

            # Seed through the ORM so column defaults apply, then release the
            # file before alembic rewrites it.
            engine = create_engine(f"sqlite:///{db_path}")
            session = sessionmaker(bind=engine)()
            try:
                session.add(User(id=1, wallet_address=WALLET))
                session.add(
                    Assessment(
                        id=1,
                        trade_history_id=None,
                        ai_score=61.0,
                        reasoning="scan verdict",
                        recommendation="hold",
                        risk_level="medium",
                        confidence=70.0,
                        ai_probability=0.55,
                        task="opportunity_scan",
                    ),
                )
                session.commit()
            finally:
                session.close()
                engine.dispose()

            downgrade = self._run(db_path, "downgrade", "20261003_0008")
            self.assertEqual(
                downgrade.returncode,
                0,
                msg=f"downgrade failed:\n{downgrade.stdout}\n{downgrade.stderr}",
            )

            tables, _, _ = self._schema(db_path, "assessments")
            # No half-finished batch rewrite left behind.
            self.assertNotIn("_alembic_tmp_assessments", tables)
            # The calibration columns are gone again…
            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(assessments)")}
            finally:
                conn.close()
            self.assertNotIn("ai_probability", columns)

            # …and the chain still goes forward afterwards.
            again = self._run(db_path, "upgrade", "head")
            self.assertEqual(again.returncode, 0, msg=f"re-upgrade failed:\n{again.stderr}")


class ServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_stop_round_trip(self):
        self.assertFalse(svc.is_equity_snapshot_running())

        class _ImmediateLoop(Exception):
            pass

        async def _noop():
            return None

        with patch.object(svc, "_snapshot_loop", _noop):
            await svc.start_equity_snapshot_service()
            self.assertTrue(svc.is_equity_snapshot_running())
            # A second start must not spawn a duplicate task.
            await svc.start_equity_snapshot_service()
            await svc.stop_equity_snapshot_service()
        self.assertFalse(svc.is_equity_snapshot_running())

    async def test_stop_is_safe_when_never_started(self):
        await svc.stop_equity_snapshot_service()
        self.assertFalse(svc.is_equity_snapshot_running())

    async def test_snapshot_loop_runs_one_cycle_then_sleeps(self):
        engine, session_factory = _make_db()
        db = session_factory()
        db.add(User(id=7, wallet_address=WALLET))
        db.commit()
        db.close()

        cycle_calls = []

        async def _fake_cycle(db):
            cycle_calls.append(db)
            return {"users": 1, "written": 0, "skipped": 1}

        async def _fake_sleep(_seconds):
            raise asyncio.CancelledError

        with (
            patch.object(svc, "run_snapshot_cycle", _fake_cycle),
            patch.object(svc, "SessionLocal", session_factory),
            patch.object(svc, "acquire_scheduler_lock", return_value=True),
            patch.object(svc, "release_scheduler_lock"),
            patch.object(svc, "scheduler_heartbeat"),
            patch("asyncio.sleep", _fake_sleep),
            contextlib.suppress(asyncio.CancelledError),
        ):
            await svc._snapshot_loop()

        self.assertEqual(len(cycle_calls), 1)
        db.close()
        engine.dispose()

    async def test_snapshot_loop_skips_start_when_lock_is_held(self):
        with (
            patch.object(svc, "acquire_scheduler_lock", return_value=False),
            patch.object(svc, "run_snapshot_cycle") as cycle,
        ):
            await svc._snapshot_loop()
        cycle.assert_not_called()

    async def test_snapshot_loop_survives_a_failing_pass(self):
        async def _bad_cycle(db):
            raise RuntimeError("db gone")

        async def _fake_sleep(_seconds):
            raise asyncio.CancelledError

        with (
            patch.object(svc, "run_snapshot_cycle", _bad_cycle),
            patch.object(svc, "SessionLocal"),
            patch.object(svc, "acquire_scheduler_lock", return_value=True),
            patch.object(svc, "release_scheduler_lock"),
            patch.object(svc, "scheduler_heartbeat"),
            patch("asyncio.sleep", _fake_sleep),
            contextlib.suppress(asyncio.CancelledError),
        ):
            await svc._snapshot_loop()
        # No exception escaped the loop.

    def test_interval_is_floored_and_configurable(self):
        with patch.object(svc, "get_settings") as settings:
            settings.return_value.equity_snapshot_interval_seconds = 60
            self.assertEqual(svc.snapshot_interval(), 60)
            settings.return_value.equity_snapshot_interval_seconds = 5
            self.assertEqual(svc.snapshot_interval(), 60)
            settings.return_value.equity_snapshot_interval_seconds = "bogus"
            self.assertEqual(svc.snapshot_interval(), svc.DEFAULT_SNAPSHOT_INTERVAL)

    def test_metrics_report_running_state_and_name(self):
        metrics = svc.get_equity_snapshot_metrics()
        self.assertEqual(metrics["scheduler_name"], "equity_snapshots")
        self.assertFalse(metrics["running"])
        self.assertGreaterEqual(metrics["interval_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
