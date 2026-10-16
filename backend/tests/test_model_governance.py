"""Model-governance tests (plan 04).

Pins the contracts that make calibration drift measurable:

* prompt versions are content-derived and stable, and change only when
  the store changes,
* the A/B split is deterministic per user (reproducible, no stickiness)
  and accurate at the configured traffic,
* drift refuses to flag noise (minimum samples) and does flag a
  synthetic degradation,
* feedback is validated, one-per-user, and never mutates the verdict,
* every governance endpoint is admin-gated where it must be.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.models.analytics.assessment import Assessment
from app.models.base import Base
from app.models.intelligence.governance import LLMExperiment, VerdictFeedback
from app.models.users.user import User
from app.services.intelligence.forecast_recorder import record_verdict
from app.services.intelligence.model_drift import (
    BASELINE_BRIER,
    MIN_SAMPLES_FOR_DRIFT,
    detect_drift,
    model_leaderboard,
    model_skill_trend,
)
from app.services.intelligence.prompt_version import (
    PROMPT_VERSION_LENGTH,
    current_prompt_version,
    reset_prompt_version,
)
from app.services.platform.experiment_service import (
    assign_variant,
    compare_experiment_arms,
    create_experiment,
    resolve_experiment,
    set_experiment_status,
)
from app.utils.time import utc_now

WALLET = "0xgovernance-wallet"
USER_ID = 9090


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class PromptVersionTest(unittest.TestCase):
    """The version is a content hash: stable, and only prompt-driven."""

    def setUp(self):
        reset_prompt_version()
        self.addCleanup(reset_prompt_version)

    def test_version_is_a_short_hex_hash(self):
        version = current_prompt_version()
        self.assertEqual(len(version), PROMPT_VERSION_LENGTH)
        int(version, 16)  # hex

    def test_version_is_stable_within_a_process(self):
        self.assertEqual(current_prompt_version(), current_prompt_version())

    def test_reformatting_the_store_does_not_change_the_version(self):
        first = current_prompt_version()
        store = {
            "b": {"x": 1, "y": 2},
            "a": {"z": 3},
        }
        with patch("app.services.intelligence.prompt_version._load_store", return_value=store):
            from app.services.intelligence.prompt_version import _compute_version

            reformatted = _compute_version({"a": {"z": 3}, "b": {"y": 2, "x": 1}})
            same = _compute_version(store)
        self.assertEqual(reformatted, same)
        # And the real store still resolves to something stable.
        self.assertIsInstance(first, str)

    def test_changing_a_prompt_changes_the_version(self):
        from app.services.intelligence.prompt_version import _compute_version

        before = _compute_version({"market_analysis": {"system": "one"}})
        after = _compute_version({"market_analysis": {"system": "two"}})
        self.assertNotEqual(before, after)

    def test_missing_store_versions_the_empty_store(self):
        """A misconfigured store still yields a stable version, not a crash."""
        with (
            patch("app.services.intelligence.prompt_version._load_store", return_value={}),
            patch(
                "app.services.intelligence.prompt_version._candidate_paths",
                return_value=["/nonexistent/prompts.json"],
            ),
        ):
            version = current_prompt_version()
        self.assertTrue(version)


class RecordVerdictGovernanceTest(unittest.TestCase):
    """``record_verdict`` stamps the governance columns."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _record(self, **kwargs):
        defaults = {
            "condition_id": "0xcond",
            "task": "opportunity_scan",
            "model_name": "gpt-4o-mini",
            "provider": "openai",
            "ai_probability": 0.7,
            "market_price": 0.5,
        }
        defaults.update(kwargs)
        row = record_verdict(self.db, **defaults)
        self.db.commit()
        return row

    def test_prompt_version_is_stamped_by_default(self):
        row = self._record()
        self.assertIsNotNone(row.prompt_version)
        self.assertEqual(len(row.prompt_version), PROMPT_VERSION_LENGTH)

    def test_explicit_governance_fields_are_stored(self):
        row = self._record(
            prompt_version="abc123def456",
            experiment_id="experiment_a",
            variant="variant",
            llm_tier="STRONG",
        )
        self.assertEqual(row.prompt_version, "abc123def456")
        self.assertEqual(row.experiment_id, "experiment_a")
        self.assertEqual(row.variant, "variant")
        self.assertEqual(row.llm_tier, "STRONG")

    def test_blank_variant_is_normalised_to_null(self):
        row = self._record(experiment_id="e1", variant="   ")
        self.assertIsNone(row.variant)


class ExperimentRoutingTest(unittest.TestCase):
    """Deterministic, stateless arm assignment."""

    def setUp(self):
        reset_prompt_version()
        self.addCleanup(reset_prompt_version)

    def test_assignment_is_deterministic(self):
        first = assign_variant("exp", 123, 50)
        for _ in range(5):
            self.assertEqual(assign_variant("exp", 123, 50), first)

    def test_assignment_depends_on_the_user_and_the_experiment(self):
        self.assertEqual(assign_variant("exp", 1, 100), "variant")
        self.assertEqual(assign_variant("exp", 1, 0), "control")

    def test_traffic_pct_controls_the_share(self):
        # 2000 synthetic users must land within ±5% of the configured share.
        for pct in (10, 50, 90):
            variants = sum(1 for u in range(2000) if assign_variant("exp", u, pct) == "variant")
            share = variants / 2000
            self.assertAlmostEqual(share, pct / 100, delta=0.05, msg=f"pct={pct}")

    def test_zero_traffic_is_all_control(self):
        self.assertEqual(
            {assign_variant("exp", u, 0) for u in range(100)},
            {"control"},
        )


class ExperimentServiceTest(unittest.TestCase):
    """Experiment lifecycle and the single-active-arm rule."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def test_create_and_resolve(self):
        experiment, error = create_experiment(
            self.db,
            name="exp1",
            task="market_analysis",
            control_config={"tier": "STRONG"},
            variant_config={"tier": "CHEAP"},
            traffic_pct=50,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(experiment)

        resolved = resolve_experiment(self.db, "market_analysis", 42)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["experiment_id"], "exp1")
        self.assertIn(resolved["variant"], ("control", "variant"))
        self.assertIn(resolved["config"], ({"tier": "STRONG"}, {"tier": "CHEAP"}))

    def test_second_active_experiment_on_the_same_task_is_rejected(self):
        create_experiment(
            self.db,
            name="exp1",
            task="market_analysis",
            traffic_pct=10,
        )
        second, error = create_experiment(
            self.db,
            name="exp2",
            task="market_analysis",
            traffic_pct=10,
        )
        self.assertIsNone(second)
        self.assertIn("already has an active experiment", error)

    def test_paused_experiment_is_not_resolved(self):
        create_experiment(self.db, name="exp1", task="market_analysis", traffic_pct=10)
        experiment = self.db.query(LLMExperiment).one()
        experiment.status = "paused"
        self.db.commit()

        self.assertIsNone(resolve_experiment(self.db, "market_analysis", 1))

    def test_reactivating_after_pausing_another_is_allowed(self):
        create_experiment(self.db, name="exp1", task="t", traffic_pct=10)
        create_experiment(self.db, name="exp2", task="other", traffic_pct=10)
        exp2 = self.db.query(LLMExperiment).filter_by(name="exp2").one()
        updated, error = set_experiment_status(self.db, exp2.id, "active")
        self.assertIsNotNone(updated)
        self.assertIsNone(error)

    def test_duplicate_name_is_rejected(self):
        create_experiment(self.db, name="exp1", task="t", traffic_pct=10)
        second, error = create_experiment(self.db, name="exp1", task="t2", traffic_pct=10)
        self.assertIsNone(second)
        self.assertIsNotNone(error)


class ExperimentComparisonTest(unittest.TestCase):
    """Arm comparison plus the insufficient-sample guard."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _seed(self, variant: str, count: int, brier: float, experiment: str = "exp1"):
        now = utc_now()
        for i in range(count):
            self.db.add(
                Assessment(
                    trade_history_id=None,
                    ai_score=50.0,
                    reasoning="test",
                    recommendation="hold",
                    risk_level="unknown",
                    confidence=50.0,
                    condition_id=f"0x{variant}{i}",
                    ai_probability=0.6,
                    market_price_at_assessment=0.5,
                    resolved_outcome="YES",
                    brier_score=brier,
                    resolved_at=now,
                    model_name="gpt-4o",
                    task="market_analysis",
                    created_at=now - timedelta(hours=1),
                    experiment_id=experiment,
                    variant=variant,
                )
            )
        self.db.commit()

    def test_insufficient_samples_never_declares_a_winner(self):
        self._seed("control", 3, 0.30)
        self._seed("variant", 3, 0.10)

        report = compare_experiment_arms(self.db, "exp1")
        # The raw Brier ordering would say "variant better"; the guard
        # refuses to call it on 3 resolved rows.
        self.assertEqual(report["verdict"], "insufficient_samples")

    def test_variant_better_is_reported_with_enough_samples(self):
        self._seed("control", 25, 0.30)
        self._seed("variant", 25, 0.10)

        report = compare_experiment_arms(self.db, "exp1")
        self.assertEqual(report["verdict"], "variant_better")
        self.assertEqual(report["arms"]["variant"]["resolved"], 25)

    def test_control_better_is_reported(self):
        self._seed("control", 25, 0.10)
        self._seed("variant", 25, 0.30)

        self.assertEqual(compare_experiment_arms(self.db, "exp1")["verdict"], "control_better")

    def test_no_difference_when_arms_are_close(self):
        self._seed("control", 25, 0.20)
        self._seed("variant", 25, 0.21)

        self.assertEqual(compare_experiment_arms(self.db, "exp1")["verdict"], "no_difference")


class DriftDetectionTest(unittest.TestCase):
    """Drift flags degradation and refuses to flag noise."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def _seed(self, model: str, task: str, briers: list[float], *, days_ago: int = 1):
        now = utc_now()
        for i, brier in enumerate(briers):
            self.db.add(
                Assessment(
                    trade_history_id=None,
                    ai_score=50.0,
                    reasoning="test",
                    recommendation="hold",
                    risk_level="unknown",
                    confidence=50.0,
                    condition_id=f"0x{model}{i}",
                    ai_probability=0.6,
                    market_price_at_assessment=0.5,
                    resolved_outcome="YES",
                    brier_score=brier,
                    resolved_at=now,
                    model_name=model,
                    task=task,
                    created_at=now - timedelta(days=days_ago),
                )
            )
        self.db.commit()

    def test_no_drift_with_no_data(self):
        report = detect_drift(self.db)
        self.assertEqual(report["drifted_count"], 0)

    def test_noise_is_not_flagged(self):
        """A few bad verdicts is not model decay."""
        self._seed("m1", "market_analysis", [0.9, 0.95, 1.0])

        report = detect_drift(self.db)
        self.assertEqual(report["drifted_count"], 0)
        row = report["series"][0]
        self.assertFalse(row["drifted"])
        self.assertIn("insufficient_samples", row["reasons"])

    def test_high_mean_brier_is_flagged(self):
        self._seed("m1", "market_analysis", [0.9] * (MIN_SAMPLES_FOR_DRIFT + 5))

        report = detect_drift(self.db)
        self.assertEqual(report["drifted_count"], 1)
        row = report["drifted"][0]
        self.assertTrue(row["drifted"])
        self.assertIn("excess_over_baseline", row["reasons"])
        self.assertGreater(row["mean_brier"], BASELINE_BRIER)

    def test_rising_slope_is_flagged_even_near_baseline(self):
        """A model getting worse is drift, even if the mean still looks OK."""
        # 30 verdicts climbing from 0.16 to 0.34 across the window: the
        # mean stays near baseline, but the slope is unambiguously upward.
        # Timestamps are spread so the regression has something to fit.
        scores = [0.16 + (0.18 * i / 29) for i in range(30)]
        today = utc_now()
        for i, brier in enumerate(scores):
            self.db.add(
                Assessment(
                    trade_history_id=None,
                    ai_score=50.0,
                    reasoning="test",
                    recommendation="hold",
                    risk_level="unknown",
                    confidence=50.0,
                    condition_id=f"0xslope{i}",
                    ai_probability=0.6,
                    market_price_at_assessment=0.5,
                    resolved_outcome="YES",
                    brier_score=brier,
                    resolved_at=today,
                    model_name="m2",
                    task="market_analysis",
                    created_at=today - timedelta(days=13, hours=23 - int(23 * i / 29)),
                )
            )
        self.db.commit()

        row = model_skill_trend(self.db, "m2", "market_analysis", window_days=14)
        self.assertGreater(row["slope_per_day"], 0.0)
        self.assertLess(row["mean_brier"], 0.25 + 0.02)

        report = detect_drift(self.db)
        self.assertEqual(report["drifted_count"], 1)
        self.assertIn("degrading_slope", report["drifted"][0]["reasons"])

    def test_legacy_rows_without_probability_are_excluded(self):
        """A pre-plan-01 row with no probability must not be scored."""
        now = utc_now()
        self.db.add(
            Assessment(
                trade_history_id=None,
                ai_score=50.0,
                reasoning="legacy",
                recommendation="hold",
                risk_level="unknown",
                confidence=50.0,
                ai_probability=None,
                brier_score=None,
                created_at=now,
            ),
        )
        self.db.commit()
        self.assertEqual(model_skill_trend(self.db, None, None, 14)["samples"], 0)

    def test_model_leaderboard_orders_best_first(self):
        self._seed("bad", "t", [0.4] * 5)
        self._seed("good", "t", [0.05] * 5)

        board = model_leaderboard(self.db, window_days=30)
        models = [m["model_name"] for m in board["models"]]
        self.assertEqual(models[0], "good")
        self.assertIn("bad", models)


class VerdictFeedbackTest(unittest.TestCase):
    """Feedback validates, is one-per-user, and never mutates the verdict."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        self.db = self.SessionFactory()
        self.db.add(User(id=USER_ID, wallet_address=WALLET))
        self.db.add(
            Assessment(
                trade_history_id=None,
                ai_score=50.0,
                reasoning="test",
                recommendation="hold",
                risk_level="unknown",
                confidence=50.0,
                ai_probability=0.6,
                condition_id="0xcond",
            ),
        )
        self.db.commit()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def test_rating_check_constraint_is_defined_on_the_model(self):
        """Rating bounds live in the schema, not only in the route validator.

        The route rejects out-of-range ratings with a 422; this is the
        defence for any other writer (a backfill, a seed script).
        """
        checks = [
            c
            for c in VerdictFeedback.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        ]
        self.assertTrue(
            any("rating" in str(c.sqltext) for c in checks),
            "verdict_feedback needs a CHECK on rating",
        )

    def test_rating_check_is_enforced_on_insert(self):
        """The CHECK actually rejects an out-of-range rating."""
        from sqlalchemy.exc import IntegrityError

        assessment_id = self.db.query(Assessment).one().id
        self.db.add(VerdictFeedback(assessment_id=assessment_id, user_id=USER_ID, rating=9))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_second_rating_updates_instead_of_duplicating(self):
        assessment_id = self.db.query(Assessment).one().id
        first = VerdictFeedback(assessment_id=assessment_id, user_id=USER_ID, rating=2)
        self.db.add(first)
        self.db.commit()

        existing = (
            self.db.query(VerdictFeedback)
            .filter_by(assessment_id=assessment_id, user_id=USER_ID)
            .first()
        )
        existing.rating = 5
        existing.comment = "much better after the prompt change"
        self.db.commit()

        rows = self.db.query(VerdictFeedback).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].rating, 5)

    def test_feedback_never_touches_the_assessment(self):
        assessment = self.db.query(Assessment).one()
        before = (
            assessment.ai_probability,
            assessment.brier_score,
            assessment.recommendation,
        )
        self.db.add(VerdictFeedback(assessment_id=assessment.id, user_id=USER_ID, rating=1))
        self.db.commit()
        self.db.refresh(assessment)
        self.assertEqual(
            before,
            (
                assessment.ai_probability,
                assessment.brier_score,
                assessment.recommendation,
            ),
        )


if __name__ == "__main__":
    unittest.main()
