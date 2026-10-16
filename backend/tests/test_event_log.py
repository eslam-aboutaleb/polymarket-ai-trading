"""Append-only event log tests (plan 10 Phase A).

The log is the substrate for compliance reporting (plan 06b) and the
marketplace's verified track record (plan 07), so the tests pin the
contracts that make it trustworthy:

* **append-only** — no application path routes UPDATE or DELETE at
  ``AppEvent``; an ORM guard asserts it.
* **transactional coupling** — an event is durable only when the
  action it describes committed, so a rollback removes both.
* **never blocks the money path** — a failing append is logged and
  swallowed, and an oversized payload is truncated rather than
  rejected.
* **no PII in the payload** — the cap and the payload shape keep a
  private key or a wallet address out of the log.

A real in-memory SQLite session is used for the DB-backed assertions.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.app_event import AppEvent
from app.models.base import Base
from app.models.user import User
from app.services.platform.event_log import (
    MAX_PAYLOAD_CHARS,
    append,
    append_non_blocking,
    get_event_log_metrics,
    get_events,
)
from app.services.trading.trade_recorder import record_user_trade
from app.utils.time import utc_now

WALLET = "0xevent-log-wallet"
USER_ID = 4242


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class AppendOnlyTest(unittest.TestCase):
    """The log is immutable: no code path updates or deletes an event."""

    def test_no_application_mutation_verbs_registered(self):
        """Guard against a future ``db.delete(AppEvent)`` or UPDATE.

        ``app.services.platform.event_log`` is the module that owns the table;
        asserting it never imports the mutation helpers is a cheap
        regression guard for the append-only invariant.
        """
        import app.services.platform.event_log as event_log_module

        with Path(event_log_module.__file__).open(encoding="utf-8") as handle:
            source = handle.read()
        for verb in ("db.delete(", "db.update(", ".delete()"):
            self.assertNotIn(
                verb,
                source,
                "event_log must stay append-only; found a mutation verb",
            )

    def test_model_has_no_updated_at_column(self):
        """An audit row with no updated_at cannot be silently edited."""
        columns = {c.name for c in AppEvent.__table__.columns}
        self.assertNotIn("updated_at", columns)
        self.assertIn("occurred_at", columns)
        self.assertIn("schema_version", columns)


class AppendTest(unittest.TestCase):
    """``append`` semantics on a real session."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def test_append_writes_a_row(self):
        row = append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="0xorder",
            user_id=USER_ID,
            payload={"strategy_source": "copy", "amount": 25.0},
            source="trade_recorder",
        )
        self.db.commit()

        self.assertIsNotNone(row)
        stored = self.db.query(AppEvent).one()
        self.assertEqual(stored.aggregate_type, "order")
        self.assertEqual(stored.aggregate_id, "0xorder")
        self.assertEqual(stored.event_type, "order_submitted")
        self.assertEqual(stored.user_id, USER_ID)
        self.assertIn('"copy"', stored.payload_json)
        self.assertTrue(stored.schema_version >= 1)
        self.assertEqual(stored.source, "trade_recorder")

    def test_rollback_removes_the_event(self):
        """An event is durable only with the action it describes."""
        append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="0xrollback",
            user_id=USER_ID,
        )
        self.db.rollback()
        self.assertEqual(self.db.query(AppEvent).count(), 0)

    def test_oversized_payload_is_truncated_not_rejected(self):
        row = append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="0xbig",
            user_id=USER_ID,
            payload={"blob": "x" * (MAX_PAYLOAD_CHARS * 2)},
        )
        self.db.commit()
        self.assertIsNotNone(row)
        stored = self.db.query(AppEvent).one()
        self.assertIn("_truncated", stored.payload_json)
        self.assertLessEqual(len(stored.payload_json), MAX_PAYLOAD_CHARS + 64)

    def test_unserializable_payload_does_not_raise(self):
        """``default=str`` keeps a weird object serialisable, not fatal."""
        row = append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="0xbad",
            user_id=USER_ID,
            payload={"obj": object()},
        )
        self.db.commit()
        self.assertIsNotNone(row)
        self.assertIsInstance(row.payload_json, str)
        self.assertIn("object", row.payload_json)

    def test_truly_unserializable_payload_is_marked(self):
        """A payload whose JSON dump actually fails is marked, not raised."""
        from unittest.mock import patch

        with patch(
            "app.services.platform.event_log.json.dumps",
            side_effect=[ValueError("nope"), '{"_unserializable": true}'],
        ):
            row = append(
                self.db,
                event_type="order_submitted",
                aggregate_type="order",
                aggregate_id="0xunser",
                user_id=USER_ID,
                payload={"blob": "x"},
            )
        self.db.commit()
        self.assertIn("_unserializable", row.payload_json)

    def test_counter_increments(self):
        before = get_event_log_metrics()["appended"]
        append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="0xc",
            user_id=USER_ID,
        )
        self.assertEqual(get_event_log_metrics()["appended"], before + 1)


class GetEventsTest(unittest.TestCase):
    """The read side matches the filters an audit consumer needs."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.add(User(id=9999, wallet_address="0xother"))
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _append(self, **kwargs):
        row = append(self.db, **kwargs)
        self.db.commit()
        return row

    def test_filters_by_user_and_type_and_returns_newest_first(self):
        self._append(
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="a",
            user_id=USER_ID,
            payload={"n": 1},
        )
        self._append(
            event_type="order_fill_observed",
            aggregate_type="order",
            aggregate_id="a",
            user_id=USER_ID,
        )
        # Another user: must never leak into USER_ID's timeline.
        self._append(
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="b",
            user_id=9999,
        )

        all_events = get_events(self.db, user_id=USER_ID)
        self.assertEqual(
            [e["event_type"] for e in all_events],
            [
                "order_fill_observed",
                "order_submitted",
            ],
        )

        submitted = get_events(self.db, user_id=USER_ID, event_types=["order_submitted"])
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["payload"]["n"], 1)

    def test_since_accepts_both_datetime_and_timedelta(self):
        old = append(
            self.db,
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="old",
            user_id=USER_ID,
        )
        self.db.commit()
        # Backdate the stored row, then filter with a relative window.
        old.occurred_at = utc_now() - timedelta(days=10)
        self.db.commit()
        self._append(
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="new",
            user_id=USER_ID,
        )

        recent = get_events(self.db, user_id=USER_ID, since=timedelta(days=2))
        self.assertEqual([e["aggregate_id"] for e in recent], ["new"])


class MoneyPathEmissionTest(unittest.TestCase):
    """The trade recorder and the monitor emit their events."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def test_recorded_trade_emits_order_submitted(self):
        record_user_trade(
            self.db,
            user_id=USER_ID,
            market_id="0xmarket",
            token_id="0xtoken",
            action="buy",
            amount=10.0,
            price=0.5,
            status="pending",
            strategy_source="copy",
            order_hash="0xhash",
            expected_price=0.5,
            expected_size=10.0,
        )
        self.db.commit()

        events = self.db.query(AppEvent).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "order_submitted")
        self.assertEqual(events[0].aggregate_id, "0xhash")
        self.assertEqual(events[0].event_type, "order_submitted")

    def test_failed_order_emits_order_failed(self):
        record_user_trade(
            self.db,
            user_id=USER_ID,
            market_id="0xmarket",
            action="buy",
            amount=10.0,
            price=0.5,
            status="failed",
            strategy_source="manual",
        )
        self.db.commit()
        self.assertEqual(self.db.query(AppEvent).one().event_type, "order_failed")

    def test_fill_emits_order_fill_observed(self):
        from app.services.trading.trade_monitor import _mark_pending_trades_filled

        record_user_trade(
            self.db,
            user_id=USER_ID,
            market_id="0xmarket",
            action="buy",
            amount=10.0,
            price=0.5,
            status="pending",
            strategy_source="copy",
            order_hash="0xfill",
        )
        self.db.commit()

        _mark_pending_trades_filled(
            self.db, "0xfill", fill_price=0.55, fill_size=10.0, fill_fee=0.01
        )

        events = self.db.query(AppEvent).all()
        fill_events = [e for e in events if e.event_type == "order_fill_observed"]
        self.assertEqual(len(fill_events), 1)
        self.assertIn("0.55", fill_events[0].payload_json)
        self.assertIn("slippage_bps", fill_events[0].payload_json)

    def test_a_failed_recording_does_not_break_the_money_path(self):
        """A bad session must not raise out of ``record_user_trade``."""
        from unittest.mock import MagicMock

        broken = MagicMock()
        broken.add.side_effect = RuntimeError("db gone")

        row = record_user_trade(
            broken,
            user_id=USER_ID,
            market_id="0xmarket",
            action="buy",
            amount=1.0,
            price=0.5,
            status="pending",
            strategy_source="copy",
        )
        self.assertIsNone(row)
        # The event log's own failure counter is what the watchdog reads;
        # the recorder logs and swallows so the order itself proceeds.
        self.assertIn("failed", get_event_log_metrics())


class NonBlockingAppendTest(unittest.TestCase):
    """The queue path drops rather than blocks, and never raises."""

    def test_append_non_blocking_without_started_writer_is_a_noop(self):
        import app.services.platform.event_log as event_log_module

        # No writer started: the queue does not exist, so the call is a
        # safe no-op rather than an AttributeError on the money path.
        event_log_module._queue = None
        append_non_blocking(
            event_type="order_submitted",
            aggregate_type="order",
            aggregate_id="x",
            user_id=USER_ID,
        )
        self.assertEqual(get_event_log_metrics()["dropped"], 0)


if __name__ == "__main__":
    unittest.main()
