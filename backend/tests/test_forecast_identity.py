"""Issue #60: immutable forecast identity, benchmark provenance and audit history.

Real in-memory SQLite. Golden fixtures cover duplicate request vs genuine
revision, wrong YES/NO token mapping, a rule/hash change, several forecasts
for one event, a quote revised after the decision, void/dispute reversals and
delayed event data. Resolution answers are the only substituted input: they
stand for the Gamma payload the issue's acceptance criteria describe.
"""

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.models.analytics.assessment import Assessment, ImmutableForecastError
from app.models.analytics.forecast_resolution_event import (
    AppendOnlyAuditError,
    ForecastResolutionEvent,
)
from app.models.base import Base
from app.services.intelligence import forecast_resolution
from app.services.intelligence.forecast_audit import resolution_history
from app.services.intelligence.forecast_identity import (
    MAX_FRESH_QUOTE_AGE_SECONDS,
    build_benchmark,
    market_provenance,
    parse_instant,
    request_key,
)
from app.services.intelligence.forecast_recorder import record_verdict
from app.services.intelligence.forecast_resolution import (
    ExclusionReason,
    MarketResolution,
    resolve_pending,
    revalidate_resolved,
)

CID = "0x" + "ab" * 32
YES_TOKEN = "111"
NO_TOKEN = "222"
MAPPING = {YES_TOKEN: "Yes", NO_TOKEN: "No"}
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _make_db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


class IdentityBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine, factory = _make_db()
        self.db = factory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def record(self, **overrides):
        kwargs = {
            "condition_id": CID,
            "task": "opportunity_scan",
            "model_name": "m1",
            "provider": "p",
            "ai_probability": 0.7,
            "market_price": 0.55,
            "prompt_version": "pv1",
            "token_id": YES_TOKEN,
            "outcome_mapping": MAPPING,
            "prediction_instant": T0,
            "rule_hash": "r" * 64,
        }
        kwargs.update(overrides)
        return record_verdict(self.db, **kwargs)

    def rows(self):
        return self.db.query(Assessment).order_by(Assessment.id).all()


class RequestIdentityTests(IdentityBase):
    def test_every_row_carries_a_uuid_a_request_key_and_the_p_interpretation(self):
        row = self.record()
        self.assertEqual(len(row.forecast_uuid), 36)
        self.assertEqual(len(row.request_key), 64)
        self.assertEqual(row.p_interpretation, "p_yes")
        self.assertEqual(row.token_id, YES_TOKEN)
        self.assertEqual(json.loads(row.outcome_mapping_json), MAPPING)
        self.assertEqual(row.rule_hash, "r" * 64)

    def test_duplicate_request_collapses_to_one_row(self):
        self.assertIsNotNone(self.record())
        self.assertIsNone(self.record())
        self.assertEqual(len(self.rows()), 1)

    def test_caller_idempotency_key_collapses_even_when_values_differ(self):
        self.assertIsNotNone(self.record(request_id="req-1"))
        self.assertIsNone(self.record(request_id="req-1", ai_probability=0.9))
        self.assertEqual(len(self.rows()), 1)

    def test_same_idempotency_key_on_another_market_is_another_request(self):
        self.assertIsNotNone(self.record(request_id="req-1"))
        self.assertIsNotNone(self.record(request_id="req-1", condition_id="0x" + "cd" * 32))
        self.assertEqual(len(self.rows()), 2)

    def test_genuine_revision_stays_distinct_even_when_rounded_fields_match(self):
        self.assertIsNotNone(self.record(ai_probability=0.7000))
        # Same rounded 0.700 / 0.550, different decision instant: a revision.
        self.assertIsNotNone(
            self.record(ai_probability=0.7000, prediction_instant=T0 + timedelta(minutes=1))
        )
        # Same everything but a new evidence snapshot / rule / calibrator.
        self.assertIsNotNone(self.record(evidence_snapshot_id="ev-2"))
        self.assertIsNotNone(self.record(rule_hash="s" * 64))
        self.assertIsNotNone(self.record(calibrator_version="cal-2"))
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(len({r.request_key for r in self.rows()}), 5)
        self.assertEqual(len({r.forecast_uuid for r in self.rows()}), 5)

    def test_replay_without_an_instant_collapses_inside_the_window_only(self):
        first = self.record(prediction_instant=None)
        self.assertIsNotNone(first)
        self.assertIsNone(self.record(prediction_instant=None))
        # The same inputs a day later are a new decision, not a replay.
        old = datetime.now(UTC) - timedelta(days=1)
        self.db.execute(
            update(Assessment)
            .where(Assessment.id == first.id)
            .values(created_at=old, request_key="x" * 64)
        )
        self.db.commit()
        self.assertIsNotNone(self.record(prediction_instant=None))
        self.assertEqual(len(self.rows()), 2)

    def test_replay_straddling_a_window_boundary_still_collapses(self):
        first = self.record(prediction_instant=None)
        # Re-key the row as if it was written in the previous replay window.
        previous_key = request_key(
            request_id=None,
            condition_id=CID,
            token_id=YES_TOKEN,
            p_interpretation="p_yes",
            probability=0.7,
            market_price=0.55,
            model="m1",
            task="opportunity_scan",
            prompt_version="pv1",
            rule_hash="r" * 64,
            evidence_snapshot_id=None,
            calibrator_version=None,
            prediction_instant=None,
            recorded_at=datetime.now(UTC),
            window_offset=-1,
        )
        self.db.execute(
            update(Assessment).where(Assessment.id == first.id).values(request_key=previous_key)
        )
        self.db.commit()
        self.assertIsNone(self.record(prediction_instant=None))
        self.assertEqual(len(self.rows()), 1)

    def test_unusable_probability_is_still_never_written_or_defaulted(self):
        self.assertIsNone(self.record(ai_probability=None))
        self.assertIsNone(self.record(ai_probability=float("nan")))
        self.assertIsNone(self.record(ai_probability=1.2))
        self.assertEqual(self.rows(), [])

    def test_stated_certainty_is_stored_as_stated_at_full_precision(self):
        row = self.record(ai_probability=1.0, market_price=0.123456)
        self.assertEqual(row.ai_probability, 1.0)
        self.assertEqual(row.market_price_at_assessment, 0.123456)


class BenchmarkProvenanceTests(IdentityBase):
    def test_contemporaneous_quote_is_recorded_with_freshness_horizon_and_group(self):
        row = self.record(
            benchmark_bid=0.54,
            benchmark_ask=0.56,
            benchmark_observed_at=T0 - timedelta(seconds=30),
            benchmark_source="clob",
            market_close_at=T0 + timedelta(hours=6),
            event_group="event-7",
        )
        self.assertAlmostEqual(row.benchmark_price, 0.55)
        self.assertEqual(row.quote_age_seconds, 30.0)
        self.assertEqual(row.benchmark_source, "clob")
        self.assertEqual(row.horizon_seconds, 6 * 3600)
        self.assertEqual(row.event_group, "event-7")

    def test_quote_revised_after_the_decision_is_not_a_matched_benchmark(self):
        row = self.record(
            benchmark_bid=0.9,
            benchmark_ask=0.92,
            benchmark_observed_at=T0 + timedelta(seconds=5),
            benchmark_source="clob",
        )
        self.assertIsNone(row.benchmark_price)
        self.assertIsNone(row.benchmark_observed_at)
        self.assertIsNone(row.quote_age_seconds)
        # The decision-time price the caller supplied is unchanged.
        self.assertEqual(row.market_price_at_assessment, 0.55)

    def test_stale_or_untimed_quote_is_not_a_matched_benchmark(self):
        stale = self.record(
            benchmark_bid=0.5,
            benchmark_ask=0.52,
            benchmark_observed_at=T0 - timedelta(seconds=MAX_FRESH_QUOTE_AGE_SECONDS + 1),
            prediction_instant=T0,
        )
        self.assertIsNone(stale.benchmark_price)
        untimed = self.record(benchmark_bid=0.5, benchmark_ask=0.52, ai_probability=0.71)
        self.assertIsNone(untimed.benchmark_price)

    def test_crossed_quote_is_rejected(self):
        bench = build_benchmark(
            price=None, bid=0.6, ask=0.5, observed_at=T0, source="x", prediction_instant=T0
        )
        self.assertFalse(bench.is_matched)
        self.assertEqual(bench.rejected_reason, "crossed_quote")

    def test_market_provenance_reads_only_what_gamma_reported(self):
        market = {
            "clobTokenIds": json.dumps([YES_TOKEN, NO_TOKEN]),
            "outcomes": json.dumps(["Yes", "No"]),
            "endDate": "2026-11-02T00:00:00Z",
            "events": [{"id": "77", "slug": "evt"}],
            "bestBid": 0.4,
            "bestAsk": 0.42,
            "updatedAt": "2026-11-01T11:59:30Z",
        }
        found = market_provenance(market)
        self.assertEqual(found["token_id"], YES_TOKEN)
        self.assertEqual(found["outcome_mapping"], MAPPING)
        self.assertEqual(found["event_group"], "77")
        self.assertEqual(found["market_close_at"], datetime(2026, 11, 2, tzinfo=UTC))
        self.assertEqual(found["benchmark_source"], "gamma")
        self.assertEqual(market_provenance({}), {})
        self.assertEqual(market_provenance(None), {})
        self.assertIsNone(parse_instant("not a date"))
        self.assertEqual(parse_instant(1_788_000_000_000), parse_instant(1_788_000_000))


class ImmutabilityTests(IdentityBase):
    def test_recorded_forecast_fields_cannot_be_edited(self):
        row = self.record()
        for name, value in (
            ("ai_probability", 0.99),
            ("market_price_at_assessment", 0.01),
            ("rule_hash", "z" * 64),
            ("token_id", NO_TOKEN),
            ("request_key", "k" * 64),
        ):
            with self.subTest(field=name):
                setattr(row, name, value)
                with self.assertRaises(ImmutableForecastError):
                    self.db.commit()
                self.db.rollback()
                self.db.refresh(row)

    def test_resolution_fields_remain_writable(self):
        row = self.record()
        row.resolved_outcome = "YES"
        row.brier_score = 0.09
        row.exclusion_reason = None
        self.db.commit()
        self.assertEqual(self.db.get(Assessment, row.id).brier_score, 0.09)

    def test_legacy_provenance_null_may_be_filled_once(self):
        row = self.record(rule_hash=None)
        row.rule_hash = "q" * 64
        self.db.commit()
        row.rule_hash = "w" * 64
        with self.assertRaises(ImmutableForecastError):
            self.db.commit()
        self.db.rollback()


def _final(outcome, *, closed_at=None, mapping=None, status="resolved"):
    return MarketResolution(
        outcome=outcome,
        closed_at=closed_at or (T0 + timedelta(days=1)),
        status=status,
        token_mapping=MAPPING if mapping is None else mapping,
    )


class ResolutionAuditTests(IdentityBase):
    async def resolve(self, market):
        with patch.object(
            forecast_resolution, "fetch_resolutions", AsyncMock(return_value={CID: market})
        ):
            return await resolve_pending(self.db)

    async def test_scoring_writes_an_append_only_event_with_finality_evidence(self):
        row = self.record(ai_probability=0.8)
        run = await self.resolve(_final("YES"))
        self.assertEqual(run.resolved, 1)
        events = resolution_history(self.db, row.id)
        self.assertEqual([e.event_type for e in events], ["scored"])
        self.assertEqual(events[0].finality_status, "resolved")
        self.assertEqual(events[0].resolved_outcome, "YES")
        self.assertEqual(events[0].scoring_version, "brier-binary-v2")
        self.assertEqual(float(events[0].brier_score), 0.8 * 0 + (0.8 - 1) ** 2)
        with self.assertRaises(AppendOnlyAuditError):
            events[0].exclusion_reason = "edited"
            self.db.commit()
        self.db.rollback()
        with self.assertRaises(AppendOnlyAuditError):
            self.db.delete(self.db.get(ForecastResolutionEvent, events[0].id))
            self.db.commit()
        self.db.rollback()

    async def test_an_unresolved_row_audits_once_per_distinct_reason(self):
        row = self.record()
        pending = MarketResolution(reason=ExclusionReason.CLOSED_NOT_FINALIZED, status="proposed")
        await self.resolve(pending)
        await self.resolve(pending)
        await self.resolve(
            MarketResolution(reason=ExclusionReason.DISPUTED_OR_CHALLENGED, status="disputed")
        )
        events = resolution_history(self.db, row.id)
        self.assertEqual(
            [e.exclusion_reason for e in events], ["closed_not_finalized", "disputed_or_challenged"]
        )

    async def test_reversed_yes_no_token_mapping_is_excluded_not_scored_backwards(self):
        row = self.record()
        flipped = {YES_TOKEN: "No", NO_TOKEN: "Yes"}
        run = await self.resolve(_final("YES", mapping=flipped))
        self.assertEqual(run.exclusions, {"token_mapping_mismatch": 1})
        self.db.refresh(row)
        self.assertIsNone(row.brier_score)

    async def test_forecast_on_the_wrong_token_is_excluded(self):
        self.record(token_id=NO_TOKEN)
        run = await self.resolve(_final("YES"))
        self.assertEqual(run.exclusions, {"token_mapping_mismatch": 1})

    async def test_late_forecast_uses_the_prediction_instant(self):
        self.record(prediction_instant=T0)
        run = await self.resolve(_final("YES", closed_at=T0 - timedelta(minutes=1)))
        self.assertEqual(run.exclusions, {"forecast_after_close": 1})

    async def test_legacy_row_without_mapping_is_scored_without_inventing_one(self):
        self.record(token_id=None, outcome_mapping=None)
        run = await self.resolve(_final("NO", mapping={}))
        self.assertEqual(run.resolved, 1)


class RevalidationTests(IdentityBase):
    async def revalidate(self, market):
        with patch.object(
            forecast_resolution, "fetch_resolutions", AsyncMock(return_value={CID: market})
        ):
            return await revalidate_resolved(self.db)

    async def score(self, probability=0.8):
        row = self.record(ai_probability=probability)
        with patch.object(
            forecast_resolution, "fetch_resolutions", AsyncMock(return_value={CID: _final("YES")})
        ):
            await resolve_pending(self.db)
        return row

    async def test_unchanged_resolution_is_revalidated_without_touching_the_score(self):
        row = await self.score()
        counts = await self.revalidate(_final("YES"))
        self.assertEqual(counts["unchanged"], 1)
        self.db.refresh(row)
        self.assertAlmostEqual(row.brier_score, 0.04)
        self.assertEqual(
            [e.event_type for e in resolution_history(self.db, row.id)], ["scored", "revalidated"]
        )

    async def test_reversed_final_outcome_is_corrected_with_both_scores_in_history(self):
        row = await self.score(0.8)
        counts = await self.revalidate(_final("NO"))
        self.assertEqual(counts["corrected"], 1)
        self.db.refresh(row)
        self.assertEqual(row.resolved_outcome, "NO")
        self.assertAlmostEqual(row.brier_score, 0.64)
        history = resolution_history(self.db, row.id)
        self.assertEqual([e.event_type for e in history], ["scored", "corrected"])
        self.assertEqual(json.loads(history[1].detail_json)["prior_outcome"], "YES")

    async def test_dispute_after_scoring_quarantines_the_row_and_leaves_aggregates(self):
        row = await self.score()
        counts = await self.revalidate(
            MarketResolution(reason=ExclusionReason.DISPUTED_OR_CHALLENGED, status="disputed")
        )
        self.assertEqual(counts["quarantined"], 1)
        self.db.refresh(row)
        self.assertEqual(row.exclusion_reason, "resolution_reversed")
        history = resolution_history(self.db, row.id)
        self.assertEqual([e.event_type for e in history], ["scored", "reversed"])
        # Quarantined rows no longer count as verified resolutions.
        from app.repositories.intelligence.assessments import AssessmentRepository

        self.assertIsNone(
            AssessmentRepository(self.db).mean_brier(
                model="m1", task="opportunity_scan", since=T0 - timedelta(days=3650)
            )
        )

    async def test_failed_lookup_changes_nothing(self):
        row = await self.score()
        counts = await self.revalidate(MarketResolution(reason=ExclusionReason.LOOKUP_FAILED))
        self.assertEqual(counts["unchanged"] + counts["corrected"] + counts["quarantined"], 0)
        self.assertEqual(len(resolution_history(self.db, row.id)), 1)


if __name__ == "__main__":
    unittest.main()
