"""Tests for app/services/forecast_recorder.py (plan 01).

Real in-memory SQLite (StaticPool so the session is shareable), with
every model registered on ``Base`` via ``app.models``. Covers the
probability bounds, the (condition_id, price, model, date) idempotency
key, and the never-backfill rule for legacy rows.
"""

import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base)
from app.models.analytics.assessment import Assessment
from app.models.base import Base
from app.services.intelligence.forecast_recorder import record_verdict
from app.utils.time import utc_now

CID = "0x" + "ab" * 32
CID_2 = "0x" + "cd" * 32


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


class RecordVerdictTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.session_factory = _make_db()
        self.db = self.session_factory()
        # LIFO cleanup: close the session before disposing the engine,
        # otherwise the session rolls back on a closed connection.
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _record(self, **overrides):
        kwargs = {
            "condition_id": CID,
            "task": "opportunity_scan",
            "model_name": "gpt-4",
            "provider": "openai",
            "ai_probability": 0.7,
            "market_price": 0.55,
            "ai_score": 72,
            "confidence": 80,
            "recommendation": "buy",
        }
        kwargs.update(overrides)
        return record_verdict(self.db, **kwargs)

    def _rows(self):
        return self.db.query(Assessment).order_by(Assessment.id).all()

    # ── probability bounds ──────────────────────────────────

    def test_missing_probability_is_not_written(self):
        self.assertIsNone(self._record(ai_probability=None))
        self.assertEqual(self._rows(), [])

    def test_out_of_range_probabilities_are_not_written(self):
        for bad in (0, 1, -0.5, 1.5, 0.0, 1.0):
            with self.subTest(bad=bad):
                self.assertIsNone(self._record(ai_probability=bad))
        self.assertEqual(self._rows(), [])

    def test_non_numeric_probability_is_not_written(self):
        self.assertIsNone(self._record(ai_probability="high"))
        self.assertEqual(self._rows(), [])

    def test_string_probability_and_price_are_coerced(self):
        record = self._record(ai_probability="0.7", market_price="0.55")
        self.assertIsNotNone(record)
        self.assertAlmostEqual(record.ai_probability, 0.7)
        self.assertAlmostEqual(record.market_price_at_assessment, 0.55)

    def test_missing_condition_id_is_not_written(self):
        self.assertIsNone(self._record(condition_id=""))
        self.assertIsNone(self._record(condition_id=None))
        self.assertEqual(self._rows(), [])

    def test_written_row_carries_calibration_fields(self):
        record = self._record()
        self.assertEqual(record.condition_id, CID)
        self.assertEqual(record.task, "opportunity_scan")
        self.assertEqual(record.model_name, "gpt-4")
        self.assertEqual(record.provider, "openai")
        self.assertEqual(record.trade_history_id, None)
        self.assertIsNone(record.resolved_outcome)
        self.assertIsNone(record.brier_score)
        self.assertIsNone(record.resolved_at)
        self.assertEqual(record.recommendation, "buy")
        self.assertEqual(record.risk_level, "unknown")
        self.assertIn("via scan", record.reasoning)

    def test_empty_model_and_provider_are_stored_as_null(self):
        record = self._record(model_name="", provider="")
        self.assertIsNone(record.model_name)
        self.assertIsNone(record.provider)

    # ── idempotency ─────────────────────────────────────────

    def test_same_key_twice_writes_one_row(self):
        self.assertIsNotNone(self._record())
        self.assertIsNone(self._record())
        self.assertEqual(len(self._rows()), 1)

    def test_rounded_price_key_dedupes(self):
        # 0.1234 and 0.12344 both round to 0.123 → same dedupe key.
        self.assertIsNotNone(self._record(market_price=0.1234))
        self.assertIsNone(self._record(market_price=0.12344))
        self.assertEqual(len(self._rows()), 1)

    def test_different_price_writes_second_row(self):
        self.assertIsNotNone(self._record(market_price=0.55))
        self.assertIsNotNone(self._record(market_price=0.62))
        self.assertEqual(len(self._rows()), 2)

    def test_different_model_writes_second_row(self):
        self.assertIsNotNone(self._record(model_name="gpt-4"))
        self.assertIsNotNone(self._record(model_name="claude"))
        self.assertEqual(len(self._rows()), 2)

    def test_different_market_writes_second_row(self):
        self.assertIsNotNone(self._record())
        self.assertIsNotNone(self._record(condition_id=CID_2))
        self.assertEqual(len(self._rows()), 2)

    def test_same_key_with_a_moved_probability_writes_second_row(self):
        """A re-analysis that changes the forecast is a new sample.

        The probability is part of the dedupe key: with it missing, a
        same-day re-scan whose forecast moved from 0.70 to 0.80 was
        silently dropped and the calibration sample kept only the stale
        number.
        """
        self.assertIsNotNone(self._record(ai_probability=0.70))
        self.assertIsNotNone(self._record(ai_probability=0.80))
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0].ai_probability, 0.7)
        self.assertAlmostEqual(rows[1].ai_probability, 0.8)

    def test_rounded_probability_key_dedupes(self):
        # 0.7004 and 0.7001 both round to 0.700 → the same sample.
        self.assertIsNotNone(self._record(ai_probability=0.7004))
        self.assertIsNone(self._record(ai_probability=0.7001))
        self.assertEqual(len(self._rows()), 1)

    def test_same_key_from_a_second_endpoint_writes_second_row(self):
        """The task label is part of the dedupe key.

        The two opportunity endpoints score the same market on the same
        day with distinct task labels; without the task in the key the
        second endpoint's verdict was dropped as a duplicate of the
        first.
        """
        self.assertIsNotNone(self._record(task="analyze-markets"))
        self.assertIsNotNone(self._record(task="opportunity-scan"))
        self.assertEqual(
            {row.task for row in self._rows()}, {"analyze-markets", "opportunity-scan"}
        )

    def test_previous_day_row_does_not_block_today(self):
        stale = Assessment(
            trade_history_id=None,
            condition_id=CID,
            task="opportunity_scan",
            ai_score=70,
            reasoning="stale",
            recommendation="buy",
            risk_level="unknown",
            confidence=80,
            ai_probability=0.7,
            market_price_at_assessment=0.55,
            model_name="gpt-4",
            provider="openai",
            created_at=utc_now() - timedelta(days=2),
        )
        self.db.add(stale)
        self.db.commit()
        self.assertIsNotNone(self._record())
        self.assertEqual(len(self._rows()), 2)

    def test_null_market_price_rows_dedupe_together(self):
        self.assertIsNotNone(self._record(market_price=None))
        self.assertIsNone(self._record(market_price=None))
        self.assertEqual(len(self._rows()), 1)

    # ── legacy rows are never backfilled ────────────────────

    def test_legacy_row_is_not_backfilled(self):
        legacy = Assessment(
            trade_history_id=None,
            ai_score=70,
            reasoning="pre-calibration verdict",
            recommendation="buy",
            risk_level="low",
            confidence=80,
            created_at=utc_now(),
        )
        self.db.add(legacy)
        self.db.commit()
        legacy_id = legacy.id

        record = self._record()
        self.assertIsNotNone(record)
        self.assertNotEqual(record.id, legacy_id)

        self.db.refresh(legacy)
        self.assertIsNone(legacy.ai_probability)
        self.assertIsNone(legacy.market_price_at_assessment)
        self.assertIsNone(legacy.model_name)
        self.assertIsNone(legacy.resolved_outcome)

    def test_record_verdict_never_updates_existing_rows(self):
        self.assertIsNotNone(self._record())
        self._record(ai_probability=0.9, market_price=0.9, model_name="claude")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0].ai_probability, 0.7)


if __name__ == "__main__":
    unittest.main()
