"""Additional coverage for :mod:`app.utils.scheduler_lock`.

Complements tests/test_scheduler_lock.py: release-failure handling,
heartbeat write failures, naive / invalid heartbeat timestamps, and
stale-heartbeat detection.
"""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from app.utils import scheduler_lock


class ReleaseSchedulerLockTests(unittest.TestCase):
    """``release_scheduler_lock`` failure handling."""

    def setUp(self):
        scheduler_lock._held_locks.clear()

    def tearDown(self):
        scheduler_lock._held_locks.clear()

    def test_release_swallows_execute_failure_and_closes_connection(self):
        connection = MagicMock()
        connection.execute.side_effect = RuntimeError("connection reset")
        scheduler_lock._held_locks["failing_scheduler"] = connection

        with patch.object(scheduler_lock, "logger") as logger_mock:
            scheduler_lock.release_scheduler_lock("failing_scheduler")

        self.assertNotIn("failing_scheduler", scheduler_lock._held_locks)
        connection.execute.assert_called_once()
        connection.close.assert_called_once_with()
        self.assertIn(
            "Failed to release scheduler lock '%s'",
            [c.args[0] for c in logger_mock.warning.call_args_list],
        )


class SchedulerHeartbeatWriteTests(unittest.TestCase):
    """``scheduler_heartbeat`` write-failure handling."""

    def test_heartbeat_write_failure_is_swallowed(self):
        cache = MagicMock()
        cache.set.side_effect = RuntimeError("cache down")

        with (
            patch.object(scheduler_lock, "_heartbeat_cache", cache),
            patch.object(scheduler_lock, "logger") as logger_mock,
        ):
            scheduler_lock.scheduler_heartbeat("broken_cache_scheduler")

        cache.set.assert_called_once_with(
            "heartbeat:broken_cache_scheduler",
            cache.set.call_args.args[1],
            scheduler_lock.HEARTBEAT_TTL_SECONDS,
        )
        self.assertTrue(logger_mock.debug.called)


class SchedulerHeartbeatAgeTests(unittest.TestCase):
    """``scheduler_heartbeat_age`` timestamp normalisation."""

    def test_age_with_naive_timestamp_is_treated_as_utc(self):
        cache = MagicMock()
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        cache.get.return_value = naive

        with patch.object(scheduler_lock, "_heartbeat_cache", cache):
            age = scheduler_lock.scheduler_heartbeat_age("naive_scheduler")

        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)

    def test_age_with_invalid_timestamp_returns_none(self):
        cache = MagicMock()
        cache.get.return_value = "not-an-iso-timestamp"

        with patch.object(scheduler_lock, "_heartbeat_cache", cache):
            self.assertIsNone(scheduler_lock.scheduler_heartbeat_age("bad_scheduler"))

    def test_age_with_recent_timestamp(self):
        cache = MagicMock()
        recent = (datetime.now(UTC) - timedelta(seconds=2)).isoformat()
        cache.get.return_value = recent

        with patch.object(scheduler_lock, "_heartbeat_cache", cache):
            age = scheduler_lock.scheduler_heartbeat_age("recent_scheduler")

        self.assertIsNotNone(age)
        self.assertLess(age, 5.0)


class SchedulerStalenessTests(unittest.TestCase):
    """``is_scheduler_stale`` end-to-end staleness detection."""

    def test_stale_heartbeat_is_reported_stale(self):
        cache = MagicMock()
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        cache.get.return_value = old

        with patch.object(scheduler_lock, "_heartbeat_cache", cache):
            self.assertTrue(scheduler_lock.is_scheduler_stale("old_scheduler"))

    def test_fresh_heartbeat_is_not_stale(self):
        cache = MagicMock()
        fresh = datetime.now(UTC).isoformat()
        cache.get.return_value = fresh

        with patch.object(scheduler_lock, "_heartbeat_cache", cache):
            self.assertFalse(scheduler_lock.is_scheduler_stale("fresh_scheduler"))


if __name__ == "__main__":
    unittest.main()
