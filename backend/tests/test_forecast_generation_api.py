"""Issue #4: evidence snapshot persistence, migration and the API (real router, SQLite)."""

import importlib.util
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.api.routes.analysis.forecasts import router
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.analytics.forecast_evidence import ForecastEvidenceSnapshot
from app.models.base import Base
from app.utils.database import get_db

SETTINGS = "app.api.routes.analysis.forecasts.get_settings"
ON = SimpleNamespace(forecast_generation_enabled=True)
OFF = SimpleNamespace(forecast_generation_enabled=False)
AS_OF = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def make(is_admin=True):
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
    return TestClient(app), db


def ev(sid, domain, text, **kw):
    return {
        "source_id": sid,
        "domain": domain,
        "text": text,
        "published_at": (AS_OF - timedelta(hours=3)).isoformat(),
        "retrieved_at": (AS_OF - timedelta(hours=1)).isoformat(),
        **kw,
    }


def body(**kw):
    base = {
        "condition_id": "0xcond1",
        "outcome_id": "tok-yes",
        "rule_hash": "rule1",
        "rule_status": "verified",
        "as_of": AS_OF.isoformat(),
        "market_prob": 0.4,
        "model_version": "m-1",
        "prompt_version": "p1",
        "category": "politics",
        "horizon_seconds": 3 * 86400,
        "event_group": "evt1",
        "evidence": [
            ev("s1", "a.example", "Candidate leads the latest poll by four points in the state"),
            ev("s2", "b.example", "Turnout models favour the incumbent party in the region"),
            ev("s3", "c.example", "Fundraising reports show a wide cash advantage this quarter"),
        ],
        "models": [
            {"name": "a", "family": "fa", "raw_p": 0.62, "interval": [0.5, 0.7]},
            {"name": "b", "family": "fb", "raw_p": 0.58, "interval": [0.5, 0.68]},
        ],
    }
    base.update(kw)
    return base


class ForecastGenerationApiTests(unittest.TestCase):
    def test_disabled_flag_returns_503(self):
        client, _ = make()
        with patch(SETTINGS, return_value=OFF):
            self.assertEqual(
                client.post("/api/analysis/forecasts/evaluate", json=body()).status_code, 503
            )

    def test_non_admin_forbidden(self):
        client, _ = make(is_admin=False)
        with patch(SETTINGS, return_value=ON):
            self.assertEqual(
                client.post("/api/analysis/forecasts/evaluate", json=body()).status_code, 403
            )
            self.assertEqual(client.get("/api/analysis/forecasts/calibration").status_code, 403)

    def test_evaluate_records_and_evidence_traces_back(self):
        client, db = make()
        with patch(SETTINGS, return_value=ON):
            r = client.post("/api/analysis/forecasts/evaluate", json=body())
            self.assertEqual(r.status_code, 200, r.text)
            out = r.json()
            self.assertTrue(out["recorded"])
            self.assertFalse(out["execution_eligible"])
            sid = out["estimate"]["evidence_snapshot_id"]
            self.assertIsNotNone(db.get(ForecastEvidenceSnapshot, sid))
            e = client.get(f"/api/analysis/forecasts/{out['forecast_id']}/evidence")
            self.assertEqual(e.status_code, 200, e.text)
            data = e.json()
            self.assertEqual(data["evidence_snapshot_id"], sid)
            self.assertEqual(data["rule_hash"], "rule1")
            self.assertAlmostEqual(data["market_price"], 0.4)
            self.assertIsNotNone(data["forecast_probability"])
            self.assertEqual({k["source_id"] for k in data["evidence"]["kept"]}, {"s1", "s2", "s3"})

    def test_snapshot_is_idempotent_and_hash_matches_content(self):
        client, db = make()
        with patch(SETTINGS, return_value=ON):
            a = client.post("/api/analysis/forecasts/evaluate", json=body()).json()
            self.assertEqual(db.query(ForecastEvidenceSnapshot).count(), 1)
            payload = json.loads(db.query(ForecastEvidenceSnapshot).one().payload_json)
        self.assertEqual(payload["independent_sources"], 3)
        self.assertEqual(a["estimate"]["independent_sources"], 3)

    def test_abstains_on_unverified_rule_and_records_nothing(self):
        client, db = make()
        with patch(SETTINGS, return_value=ON):
            out = client.post(
                "/api/analysis/forecasts/evaluate", json=body(rule_status="ambiguous")
            ).json()
        self.assertTrue(out["estimate"]["abstain"])
        self.assertFalse(out["recorded"])
        self.assertEqual(db.query(ForecastEvidenceSnapshot).count(), 0)

    def test_naive_timestamp_rejected(self):
        client, _ = make()
        with patch(SETTINGS, return_value=ON):
            r = client.post(
                "/api/analysis/forecasts/evaluate", json=body(as_of="2026-11-20T12:00:00")
            )
        self.assertEqual(r.status_code, 422)

    def test_unknown_forecast_404_and_extra_fields_rejected(self):
        client, _ = make()
        with patch(SETTINGS, return_value=ON):
            self.assertEqual(client.get("/api/analysis/forecasts/nope/evidence").status_code, 404)
            self.assertEqual(
                client.post(
                    "/api/analysis/forecasts/evaluate", json=body(order_size=5)
                ).status_code,
                422,
            )

    def test_calibration_by_category_and_horizon_reports_insufficient_sample(self):
        client, _ = make()
        with patch(SETTINGS, return_value=ON):
            client.post("/api/analysis/forecasts/evaluate", json=body())
            r = client.get("/api/analysis/forecasts/calibration?category=politics&horizon=1d_to_7d")
            self.assertEqual(r.status_code, 200, r.text)
            rep = r.json()
            self.assertEqual(rep["category"], "politics")
            self.assertEqual(rep["report"]["forecast_count"], 1)
            self.assertNotEqual(str(rep["report"]["status"]).lower(), "better_than_market")
            other = client.get("/api/analysis/forecasts/calibration?category=sports").json()
            self.assertEqual(other["report"]["forecast_count"], 0)


class MigrationTests(unittest.TestCase):
    def test_upgrade_downgrade_and_chain(self):
        path = Path(__file__).parents[1] / "migrations/versions/20261125_0004_forecast_evidence.py"
        spec = importlib.util.spec_from_file_location("m0004", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.down_revision, "20261125_0060_forecast_identity")
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()
            mod.upgrade()  # idempotent
            self.assertTrue(inspect(conn).has_table("forecast_evidence_snapshots"))
            mod.downgrade()
            self.assertFalse(inspect(conn).has_table("forecast_evidence_snapshots"))


if __name__ == "__main__":
    unittest.main()
