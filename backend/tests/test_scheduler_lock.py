"""Tests for the named advisory-lock and heartbeat helpers.

The database engine is mocked so the suite runs without a live
Postgres instance; heartbeat round-trips use the real cache layer
(in-memory when REDIS_URL is unset).
"""

import unittest
from unittest.mock import MagicMock, patch

from app.utils import scheduler_lock


class SchedulerLockTests(unittest.TestCase):
    """Advisory-lock acquisition, release and fail-open behaviour."""

    def setUp(self):
        scheduler_lock._held_locks.clear()

    def tearDown(self):
        scheduler_lock._held_locks.clear()

    def test_acquire_success(self):
        connection = MagicMock()
        connection.execute.return_value.scalar.return_value = True
        with patch.object(scheduler_lock, "engine") as mock_engine:
            mock_engine.connect.return_value = connection
            self.assertTrue(scheduler_lock.acquire_scheduler_lock("test_scheduler"))
            self.assertIn("test_scheduler", scheduler_lock._held_locks)
            connection.close.assert_not_called()

    def test_acquire_contended(self):
        connection = MagicMock()
        connection.execute.return_value.scalar.return_value = False
        with patch.object(scheduler_lock, "engine") as mock_engine:
            mock_engine.connect.return_value = connection
            self.assertFalse(scheduler_lock.acquire_scheduler_lock("test_scheduler"))
            self.assertNotIn("test_scheduler", scheduler_lock._held_locks)
            connection.close.assert_called()

    def test_acquire_is_idempotent(self):
        scheduler_lock._held_locks["test_scheduler"] = MagicMock()
        self.assertTrue(scheduler_lock.acquire_scheduler_lock("test_scheduler"))

    def test_acquire_database_error_fails_open(self):
        with patch.object(scheduler_lock, "engine") as mock_engine:
            mock_engine.connect.side_effect = RuntimeError("db down")
            self.assertTrue(scheduler_lock.acquire_scheduler_lock("test_scheduler"))

    def test_release(self):
        connection = MagicMock()
        scheduler_lock._held_locks["test_scheduler"] = connection
        scheduler_lock.release_scheduler_lock("test_scheduler")
        self.assertNotIn("test_scheduler", scheduler_lock._held_locks)
        connection.execute.assert_called()
        connection.close.assert_called()

    def test_release_unknown_is_noop(self):
        scheduler_lock.release_scheduler_lock("never_acquired")


class SchedulerHeartbeatTests(unittest.TestCase):
    """Dead-man's-switch heartbeat write/read and staleness logic."""

    def test_heartbeat_roundtrip(self):
        scheduler_lock.scheduler_heartbeat("hb_test_scheduler")
        age = scheduler_lock.scheduler_heartbeat_age("hb_test_scheduler")
        self.assertIsNotNone(age)
        self.assertLess(age, 5.0)

    def test_heartbeat_age_unknown_scheduler(self):
        self.assertIsNone(scheduler_lock.scheduler_heartbeat_age("never_seen_scheduler"))

    def test_stale_threshold_uses_interval_map(self):
        # stop_loss_monitor: max(3 * 10, 120) = 120
        self.assertEqual(scheduler_lock.scheduler_stale_threshold("stop_loss_monitor"), 120)
        # arbitrage_monitor: 3 * 120 = 360
        self.assertEqual(scheduler_lock.scheduler_stale_threshold("arbitrage_monitor"), 360)
        # unknown scheduler: max(3 * 300, 120) = 900
        self.assertEqual(scheduler_lock.scheduler_stale_threshold("unknown_scheduler"), 900)

    def test_is_scheduler_stale(self):
        self.assertIsNone(scheduler_lock.is_scheduler_stale("never_seen_scheduler"))
        scheduler_lock.scheduler_heartbeat("stale_test_scheduler")
        self.assertFalse(scheduler_lock.is_scheduler_stale("stale_test_scheduler"))
