"""Issue #22: skill report over recorded forecasts, end to end through the route.

Real in-memory SQLite and the real FastAPI router. Rows are written exactly as
the recorder and resolver write them (identity columns, verified resolution).
"""

import random
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.routes.analysis.calibration import router
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.analytics.assessment import VERIFIED_SCORING_VERSION, Assessment
from app.models.base import Base
from app.services.intelligence.forecast_skill_report import horizon_bucket, to_score_input
from app.utils.database import get_db
from app.utils.time import utc_now


def _make_app_and_db(is_admin=False):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_from_token] = lambda: {
        "user_id": 1,
        "wallet_address": "0x" + "ab" * 20,
        "is_admin": is_admin,
    }
    app.dependency_overrides[get_db] = lambda: db
    return app, TestClient(app), db, engine


ROUTE = "app.api.routes.analysis.calibration.get_settings"
ENABLED = SimpleNamespace(forecast_calibration_enabled=True)


class SkillReportTests(unittest.TestCase):
    def setUp(self):
        self.app, self.client, self.db, self.engine = _make_app_and_db(is_admin=True)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def add(self, i, p, outcome, *, base=0.5, matched=True, event=None, model="m1", **kw):
        now = utc_now()
        row = Assessment(
            trade_history_id=None,
            condition_id=f"0x{i:064x}",
            ai_score=70,
            reasoning="r",
            recommendation="buy",
            risk_level="unknown",
            confidence=80,
            ai_probability=p,
            market_price_at_assessment=base,
            model_name=model,
            provider="p",
            task="t",
            created_at=now - timedelta(days=2, minutes=i),
            prediction_instant=now - timedelta(days=2, minutes=i),
            benchmark_price=base if matched else None,
            benchmark_observed_at=(now - timedelta(days=2, minutes=i, seconds=5))
            if matched
            else None,
            event_group=event or f"ev{i}",
            horizon_seconds=3 * 86400,
            resolved_outcome=outcome,
            brier_score=(p - (1.0 if outcome == "YES" else 0.0)) ** 2 if outcome else None,
            resolved_at=now - timedelta(days=1) if outcome else None,
            scoring_version=VERIFIED_SCORING_VERSION if outcome else None,
            **kw,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def get(self, **params):
        with patch(ROUTE, return_value=ENABLED):
            return self.client.get(
                "/api/analysis/calibration/skill-report", params={"scope": "global", **params}
            )

    def test_disabled_by_default_and_global_scope_needs_admin(self):
        with patch(ROUTE, return_value=SimpleNamespace(forecast_calibration_enabled=False)):
            self.assertEqual(
                self.client.get("/api/analysis/calibration/skill-report").status_code, 503
            )
        app, client, db, engine = _make_app_and_db(is_admin=False)
        self.addCleanup(engine.dispose)
        self.addCleanup(db.close)
        with patch(ROUTE, return_value=ENABLED):
            r = client.get("/api/analysis/calibration/skill-report", params={"scope": "global"})
        self.assertEqual(r.status_code, 403)

    def test_empty_window_abstains_explicitly(self):
        body = self.get().json()
        self.assertEqual(body["overall"]["abstain_reason"], "NO_SCORABLE_FORECASTS")
        self.assertIs(body["execution_eligible"], False)

    def test_informative_model_with_matched_baselines_beats_market_with_ci(self):
        rng = random.Random(3)  # noqa: S311
        for i in range(60):
            y = rng.random() < 0.5
            self.add(i, 0.8 if y else 0.2, "YES" if y else "NO")
        body = self.get().json()
        self.assertEqual(body["overall"]["status"], "better_than_market")
        self.assertGreater(body["overall"]["paired_brier_delta_ci95"][0], 0)
        self.assertEqual(body["overall"]["effective_independent_events"], 60)
        self.assertEqual(body["by_model"]["m1"]["state"]["research_only"], False)
        self.assertEqual(body["by_horizon"]["1d_to_7d"]["scored_forecast_count"], 60)
        self.assertEqual(body["source_coverage"]["with_matched_baseline"], 60)

    def test_unmatched_baselines_never_produce_a_skill_claim(self):
        for i in range(40):
            self.add(i, 0.8, "YES", matched=False)
        overall = self.get().json()["overall"]
        self.assertEqual(overall["status"], "no_matched_baseline")
        self.assertNotIn("paired_brier_delta", overall)

    def test_overlapping_markets_of_one_event_count_once(self):
        for i in range(10):
            self.add(i, 0.9, "YES", event="same")
        overall = self.get().json()["overall"]
        self.assertEqual(overall["scored_forecast_count"], 10)
        self.assertEqual(overall["effective_independent_events"], 1)
        self.assertEqual(overall["status"], "insufficient_sample")

    def test_unresolved_and_void_rows_are_excluded_with_a_reason(self):
        self.add(1, 0.7, None)
        self.add(2, 0.7, "YES", exclusion_reason="resolution_reversed")
        self.add(3, 0.7, "YES")
        excluded = self.get().json()["overall"]["excluded"]
        self.assertEqual(excluded, {"no_final_label": 2})

    def test_drifted_model_is_research_only_even_with_skill(self):
        rng = random.Random(4)  # noqa: S311
        for i in range(60):
            y = rng.random() < 0.5
            self.add(i, 0.8 if y else 0.2, "YES" if y else "NO")
        with patch(
            "app.services.intelligence.forecast_skill_report.detect_drift",
            return_value={"drifted": [{"model_name": "m1"}], "drifted_count": 1},
        ):
            body = self.get().json()
        self.assertTrue(body["by_model"]["m1"]["state"]["research_only"])
        self.assertIn("MODEL_DRIFT", body["by_model"]["m1"]["state"]["reasons"])

    def test_adapter_marks_token_mapping_mismatch_and_horizon_buckets(self):
        row = self.add(1, 0.7, None, exclusion_reason="token_mapping_mismatch")
        self.assertTrue(to_score_input(row).mapping_reversed)
        self.assertEqual(
            [horizon_bucket(s) for s in (None, -1, 100, 86400, 8 * 86400)],
            ["unknown", "unknown", "under_1d", "1d_to_7d", "over_7d"],
        )


if __name__ == "__main__":
    unittest.main()
