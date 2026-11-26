import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_cross_market_consistency import END, T0, mk

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.markets import consistency
from app.config import get_settings


class ConsistencyApi(unittest.TestCase):
    def client(self, enabled):
        app = FastAPI()
        app.include_router(consistency.router)
        app.dependency_overrides[get_current_user_from_token] = lambda: object()
        s = get_settings().model_copy(update={"cross_market_consistency_enabled": enabled})
        p = patch.object(consistency, "get_settings", return_value=s)
        p.start()
        self.addCleanup(p.stop)
        return TestClient(app)

    def body(self):
        return {
            "as_of": T0.isoformat(),
            "markets": [
                {
                    **mk(0, "0.70", "0.72"),
                    "end_time": END.isoformat(),
                    "snapshot_at": T0.isoformat(),
                },
                {
                    **mk(1, "0.40", "0.42"),
                    "end_time": END.isoformat(),
                    "snapshot_at": T0.isoformat(),
                },
            ],
            "relation": {
                "relation_id": "r",
                "template": "implication",
                "members": ["m0", "m1"],
                "verified": True,
            },
        }

    def test_disabled_flag_returns_503(self):
        self.assertEqual(
            self.client(False).post("/api/v1/consistency/analyze", json=self.body()).status_code,
            503,
        )

    def test_enabled_returns_non_executable_alert(self):
        c = self.client(True)
        r = c.post("/api/v1/consistency/analyze", json=self.body())
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["classification"], "logical_inconsistency")
        self.assertIs(j["executable"], False)
        self.assertEqual(c.get("/api/v1/consistency/schema").status_code, 200)

    def test_extra_fields_rejected(self):
        b = self.body()
        b["wallet_key"] = "x"
        self.assertEqual(
            self.client(True).post("/api/v1/consistency/analyze", json=b).status_code, 422
        )

    def test_no_trading_imports(self):
        import inspect

        import app.services.analytics.consistency.cross_market_consistency as m

        src = inspect.getsource(m) + inspect.getsource(consistency)
        for banned in ("pre_trade_gate", "live_trading_policy", "submit_order", "private_key"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
