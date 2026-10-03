"""Coverage for the inverse-bot routes (``/api/inverse-bot``).

Exercises listing, upsert (create, update and error-state
recovery), disable, manual evaluation (success, failure and
default-detail paths), the diagnostics metrics endpoint,
request validation, and the response serializer. Position
rows are persisted in an in-memory SQLite database.
"""

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.inverse_bot import _to_response
from app.main import app
from app.models.base import Base
from app.models.inverse_bot_position import InverseBotPosition
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _position_body(**overrides):
    base = {
        "token_id": "0xtoken1",
        "condition_id": "0xcond1",
        "market_title": "Will BTC go up?",
        "outcome": "yes",
        "enabled": True,
        "size_mode_override": "inherit",
        "fixed_amount_override": None,
    }
    base.update(overrides)
    return base


class InverseBotRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.factory = _session_factory()
        self.session = self.factory()
        app.dependency_overrides[get_db] = lambda: self.session
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": WALLET,
            "is_admin": False,
        }
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def _add_position(self, user_id=1, **overrides):
        values = _position_body(**overrides)
        row = InverseBotPosition(user_id=user_id, **values)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row


class ListPositionsTests(InverseBotRouteTestCase):
    def test_list_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/inverse-bot/positions")
        self.assertEqual(response.status_code, 401)

    def test_list_returns_user_positions_newest_first(self):
        self._add_position(token_id="0xolder")
        newer = self._add_position(token_id="0xnewer")
        self._add_position(user_id=2, token_id="0xother")
        response = self.client.get("/api/inverse-bot/positions")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["token_id"], "0xnewer")
        self.assertEqual(body[1]["token_id"], "0xolder")
        self.assertEqual(body[0]["id"], newer.id)
        self.assertEqual(body[0]["status"], "active")
        self.assertTrue(body[0]["enabled"])
        self.assertEqual(body[0]["size_mode_override"], "inherit")
        self.assertIsNone(body[0]["fixed_amount_override"])
        self.assertIsNone(body[0]["last_signal"])
        self.assertEqual(body[0]["reversals_today"], 0)
        self.assertEqual(body[0]["persistence_count"], 0)
        self.assertTrue(body[0]["created_at"])

    def test_list_empty(self):
        response = self.client.get("/api/inverse-bot/positions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


class UpsertPositionTests(InverseBotRouteTestCase):
    def test_upsert_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.post("/api/inverse-bot/positions", json=_position_body())
        self.assertEqual(response.status_code, 401)

    def test_upsert_creates_new_position(self):
        response = self.client.post(
            "/api/inverse-bot/positions",
            json=_position_body(fixed_amount_override=25.0),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["token_id"], "0xtoken1")
        self.assertEqual(body["condition_id"], "0xcond1")
        self.assertEqual(body["market_title"], "Will BTC go up?")
        self.assertEqual(body["outcome"], "yes")
        self.assertTrue(body["enabled"])
        self.assertEqual(body["fixed_amount_override"], 25.0)
        self.assertEqual(body["status"], "active")
        rows = self.session.query(InverseBotPosition).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].user_id, 1)

    def test_upsert_updates_existing_position(self):
        existing = self._add_position(market_title="Old title", outcome="no")
        response = self.client.post(
            "/api/inverse-bot/positions",
            json=_position_body(
                market_title="New title",
                outcome="yes",
                enabled=False,
            ),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], existing.id)
        self.assertEqual(body["market_title"], "New title")
        self.assertEqual(body["outcome"], "yes")
        self.assertFalse(body["enabled"])
        rows = self.session.query(InverseBotPosition).all()
        self.assertEqual(len(rows), 1)

    def test_upsert_recovers_from_error_state_when_enabled(self):
        existing = self._add_position()
        existing.status = "error"
        existing.last_error = "boom"
        self.session.commit()
        response = self.client.post(
            "/api/inverse-bot/positions",
            json=_position_body(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "active")
        self.assertIsNone(body["last_error"])
        self.session.refresh(existing)
        self.assertEqual(existing.status, "active")
        self.assertIsNone(existing.last_error)

    def test_upsert_keeps_error_state_when_disabled(self):
        existing = self._add_position()
        existing.status = "error"
        existing.last_error = "boom"
        self.session.commit()
        response = self.client.post(
            "/api/inverse-bot/positions",
            json=_position_body(enabled=False),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["last_error"], "boom")

    def test_upsert_validates_body(self):
        for body in (
            {"token_id": "x", "condition_id": "0xcond1"},
            {"token_id": "0xtoken1", "condition_id": "x"},
            {"token_id": "0xtoken1", "condition_id": "0xcond1", "size_mode_override": "bogus"},
            {"token_id": "0xtoken1", "condition_id": "0xcond1", "fixed_amount_override": 0.0},
            {"token_id": "0xtoken1", "condition_id": "0xcond1", "fixed_amount_override": -1.0},
        ):
            response = self.client.post("/api/inverse-bot/positions", json=body)
            self.assertEqual(response.status_code, 422, body)


class DisablePositionTests(InverseBotRouteTestCase):
    def test_disable_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.delete("/api/inverse-bot/positions/1")
        self.assertEqual(response.status_code, 401)

    def test_disable_existing_position(self):
        row = self._add_position()
        response = self.client.delete(f"/api/inverse-bot/positions/{row.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "disabled", "id": row.id})
        self.session.refresh(row)
        self.assertFalse(row.enabled)

    def test_disable_missing_position_returns_404(self):
        response = self.client.delete("/api/inverse-bot/positions/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Inverse bot position not found")

    def test_disable_other_users_position_returns_404(self):
        row = self._add_position(user_id=2)
        response = self.client.delete(f"/api/inverse-bot/positions/{row.id}")
        self.assertEqual(response.status_code, 404)


class EvaluatePositionTests(InverseBotRouteTestCase):
    def test_evaluate_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.post("/api/inverse-bot/positions/1/evaluate")
        self.assertEqual(response.status_code, 401)

    def test_evaluate_success(self):
        row = self._add_position()
        with patch(
            "app.api.routes.inverse_bot.manual_evaluate_inverse_position",
            new_callable=AsyncMock,
            return_value={"success": True, "result": {"signal": "buy"}},
        ) as evaluate:
            response = self.client.post(f"/api/inverse-bot/positions/{row.id}/evaluate")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["result"], {"signal": "buy"})
        self.assertIsNone(body["detail"])
        evaluate.assert_awaited_once_with(self.session, 1, row.id)

    def test_evaluate_failure_with_detail(self):
        row = self._add_position()
        with patch(
            "app.api.routes.inverse_bot.manual_evaluate_inverse_position",
            new_callable=AsyncMock,
            return_value={"success": False, "detail": "not found"},
        ):
            response = self.client.post(f"/api/inverse-bot/positions/{row.id}/evaluate")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertIsNone(body["result"])
        self.assertEqual(body["detail"], "not found")

    def test_evaluate_failure_without_detail_uses_default(self):
        row = self._add_position()
        with patch(
            "app.api.routes.inverse_bot.manual_evaluate_inverse_position",
            new_callable=AsyncMock,
            return_value={"success": False},
        ):
            response = self.client.post(f"/api/inverse-bot/positions/{row.id}/evaluate")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["detail"], "Evaluation failed")


class MetricsRouteTests(InverseBotRouteTestCase):
    def test_metrics_requires_authentication(self):
        app.dependency_overrides.clear()
        response = self.client.get("/api/inverse-bot/metrics")
        self.assertEqual(response.status_code, 401)

    def test_metrics_returns_monitor_metrics(self):
        with patch(
            "app.api.routes.inverse_bot.get_inverse_bot_metrics",
            return_value={"positions_tracked": 3, "evaluations": 7},
        ):
            response = self.client.get("/api/inverse-bot/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"positions_tracked": 3, "evaluations": 7},
        )


class ResponseHelperTests(unittest.TestCase):
    def test_to_response_with_full_row(self):
        now = datetime.now()
        row = InverseBotPosition(
            id=5,
            user_id=1,
            token_id="0xtoken1",
            condition_id="0xcond1",
            market_title="Title",
            outcome="yes",
            enabled=True,
            size_mode_override="fixed_amount",
            fixed_amount_override=10.0,
            status="cooldown",
            last_signal="buy",
            last_confidence=0.8,
            last_reasoning="edge",
            last_web_summary="web",
            last_x_summary="x",
            last_error=None,
            last_recommendation="hold",
            last_alt_outcome="no",
            last_alt_token_id="0xalt",
            last_evaluated_at=now,
            last_reversed_at=now,
            reversals_today=2,
            reversals_day=now.date(),
            persistence_count=4,
            created_at=now,
            updated_at=now,
        )
        response = _to_response(row)
        self.assertEqual(response.id, 5)
        self.assertEqual(response.status, "cooldown")
        self.assertEqual(response.size_mode_override, "fixed_amount")
        self.assertEqual(response.last_evaluated_at, now.isoformat())
        self.assertEqual(response.last_reversed_at, now.isoformat())
        self.assertEqual(response.reversals_day, now.date().isoformat())
        self.assertEqual(response.updated_at, now.isoformat())
        self.assertEqual(response.reversals_today, 2)
        self.assertEqual(response.persistence_count, 4)

    def test_to_response_with_minimal_row(self):
        row = InverseBotPosition(
            id=1,
            user_id=1,
            token_id="0xtoken1",
            condition_id="0xcond1",
        )
        response = _to_response(row)
        self.assertEqual(response.market_title, "")
        self.assertEqual(response.outcome, "")
        self.assertEqual(response.status, "active")
        self.assertEqual(response.size_mode_override, "inherit")
        self.assertIsNone(response.last_evaluated_at)
        self.assertIsNone(response.last_reversed_at)
        self.assertIsNone(response.reversals_day)
        self.assertIsNone(response.updated_at)
        self.assertEqual(response.created_at, "")


if __name__ == "__main__":
    unittest.main()
