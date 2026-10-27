import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_latency_arb_service_coverage import _opportunity

from app.api.routes.auth.auth import get_current_user_from_token
from app.api.routes.trading import latency_arb as route
from app.services.trading import latency_arb_service as runner
from app.utils.cache import InMemoryCache
from app.utils.metrics import counter


def test_rejected_evidence_visible_on_real_api_only_to_its_user(monkeypatch, caplog):
    monkeypatch.setattr(runner, "_execution_outcome_cache", InMemoryCache("test-outcomes"))
    metric = counter("latency_book_provider_http_503_total")
    before = metric.value
    with caplog.at_level(logging.INFO, logger=runner.__name__):
        runner._record_execution_outcome(
            7, _opportunity(), {"status": "rejected", "reason": "PROVIDER_HTTP_503"}
        )
    assert metric.value == before + 1
    assert "PROVIDER_HTTP_503" in caplog.text
    app = FastAPI()
    app.include_router(route.router)
    monkeypatch.setattr(route, "get_latest_opportunities", list)
    app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 7}
    with TestClient(app) as client:
        outcome = client.get("/api/latency-arb/opportunities").json()["execution_outcome"]
        assert outcome["reason"] == "PROVIDER_HTTP_503"
        assert (
            outcome["condition_id"] and outcome["token_id"] and outcome["at"] and outcome["source"]
        )
        app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 8}
        assert client.get("/api/latency-arb/opportunities").json()["execution_outcome"] is None
