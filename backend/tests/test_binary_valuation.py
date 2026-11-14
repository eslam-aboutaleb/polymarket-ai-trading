"""Unit, boundary, API, and MCP tests for binary-contract-valuation (#15)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.main import app as fastapi_app
from app.services.intelligence.binary_valuation import (
    BinaryValuationInput,
    value_binary_contract,
)

CID = "0x" + "b" * 64
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _base_input(**overrides) -> BinaryValuationInput:
    data = {
        "condition_id": CID,
        "token_id": "101",
        "outcome": "YES",
        "side": "BUY",
        "shares": Decimal("100"),
        "as_of": NOW,
        "rules_hash": "a" * 64,
        "rule_review_status": "verified",
        "calibrated_p_yes": Decimal("0.60"),
        "p_yes_lower": Decimal("0.56"),
        "p_yes_upper": Decimal("0.64"),
        "executable_vwap": Decimal("0.55"),
        "available_depth_shares": Decimal("250"),
        "taker_fee_per_share": Decimal("0.01"),
        "fee_schedule_version": "explicit-flat-assumption-v1",
        "market_implied_p": Decimal("0.55"),
    }
    data.update(overrides)
    return BinaryValuationInput(**data)


def test_worked_fixture_costs_exceeding_gross_edge_turn_net_negative():
    res = value_binary_contract(_base_input(taker_fee_per_share=Decimal("0.06")))
    assert res.status == "valued"
    assert res.decision == "research_only"
    assert res.execution_eligible is False
    assert res.ev_gross_per_share == Decimal("0.05")
    assert res.ev_net_per_share == Decimal("-0.01")
    assert res.ev_net_total == Decimal("-1.00")
    assert res.bounded_kelly_fraction == Decimal("0")
    assert res.break_even_probability == Decimal("0.61")


def test_positive_net_edge_market_baseline_and_sensitivity_grid():
    res = value_binary_contract(_base_input(taker_fee_per_share=Decimal("0.01")))
    assert res.status == "valued"
    assert res.ev_gross_per_share == Decimal("0.05")
    assert res.ev_net_per_share == Decimal("0.04")
    assert res.ev_net_lower_bound == Decimal("0.00")
    assert res.market_baseline_ev_per_share == Decimal("-0.01")
    assert res.incremental_edge_vs_market == Decimal("0.05")
    assert len(res.sensitivity_grid) == 6


def test_boundary_probabilities_asymmetric_no_and_sell_exit():
    # Boundary p=0 and p=1.
    p0 = value_binary_contract(
        _base_input(
            calibrated_p_yes=Decimal("0"),
            p_yes_lower=Decimal("0"),
            p_yes_upper=Decimal("0"),
        )
    )
    assert p0.ev_gross_per_share == Decimal("-0.55")

    p1 = value_binary_contract(
        _base_input(
            calibrated_p_yes=Decimal("1"),
            p_yes_lower=Decimal("1"),
            p_yes_upper=Decimal("1"),
        )
    )
    assert p1.ev_gross_per_share == Decimal("0.45")

    # Asymmetric NO book: p_yes = 0.30 -> p_no = 0.70, NO ask = 0.62, fee = 0.01 -> net +0.07.
    no_buy = value_binary_contract(
        _base_input(
            outcome="NO",
            calibrated_p_yes=Decimal("0.30"),
            p_yes_lower=Decimal("0.25"),
            p_yes_upper=Decimal("0.35"),
            executable_vwap=Decimal("0.62"),
            taker_fee_per_share=Decimal("0.01"),
        )
    )
    assert no_buy.ev_gross_per_share == Decimal("0.08")
    assert no_buy.ev_net_per_share == Decimal("0.07")

    # SELL exit: p_yes = 0.40, bid = 0.50, fee = 0.01 -> gross +0.10, net +0.09.
    yes_sell = value_binary_contract(
        _base_input(
            side="SELL",
            owned_shares=Decimal("100"),
            calibrated_p_yes=Decimal("0.40"),
            p_yes_lower=Decimal("0.35"),
            p_yes_upper=Decimal("0.45"),
            executable_vwap=Decimal("0.50"),
            taker_fee_per_share=Decimal("0.01"),
        )
    )
    assert yes_sell.ev_gross_per_share == Decimal("0.10")
    assert yes_sell.ev_net_per_share == Decimal("0.09")


def test_abstains_on_unverified_rule_insufficient_depth_and_negative_fee():
    unverified = value_binary_contract(_base_input(rule_review_status="ambiguous"))
    assert unverified.status == "abstain"
    assert "ABSTAIN_UNVERIFIED_OR_AMBIGUOUS_RULE" in unverified.abstain_reasons

    no_depth = value_binary_contract(_base_input(available_depth_shares=Decimal("50")))
    assert no_depth.status == "abstain"
    assert "ABSTAIN_INSUFFICIENT_DEPTH" in no_depth.abstain_reasons

    neg_fee = value_binary_contract(_base_input(taker_fee_per_share=Decimal("-0.01")))
    assert neg_fee.status == "abstain"
    assert "ABSTAIN_NEGATIVE_OR_NONFINITE_COST" in neg_fee.abstain_reasons


def test_binary_valuation_api_endpoints(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.models import Base
    from app.utils.database import get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    fastapi_app.dependency_overrides[get_db] = lambda: db
    fastapi_app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    client = TestClient(fastapi_app)
    get_settings.cache_clear()
    try:
        assert client.get("/api/valuation/schema").status_code == 503

        monkeypatch.setenv("BINARY_VALUATION_ENABLED", "true")
        get_settings.cache_clear()

        assert client.get("/api/valuation/schema").status_code == 200
        resp = client.post(
            "/api/valuation/binary",
            json={
                "condition_id": CID,
                "token_id": "101",
                "outcome": "YES",
                "side": "BUY",
                "shares": "100",
                "as_of": "2026-11-13T12:00:00Z",
                "rules_hash": "a" * 64,
                "rule_review_status": "verified",
                "calibrated_p_yes": "0.60",
                "executable_vwap": "0.55",
                "available_depth_shares": "250",
                "taker_fee_per_share": "0.06",
                "fee_schedule_version": "explicit-flat-assumption-v1",
            },
        )
        assert resp.status_code == 422
        resp = client.post(
            "/api/valuation/binary",
            json={
                "condition_id": CID,
                "token_id": "101",
                "outcome": "YES",
                "shares": "100",
                "as_of": NOW.isoformat(),
                "rules_hash": "a" * 64,
                "forecast_id": 1,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "abstain"
        assert "ABSTAIN_UNVERIFIED_RECORDED_RULE" in body["abstain_reasons"]
        assert body["ev_net_per_share"] is None
        assert body["execution_eligible"] is False
    finally:
        fastapi_app.dependency_overrides.clear()
        get_settings.cache_clear()
        db.close()
        engine.dispose()


def test_research_mcp_binary_valuation_tool(monkeypatch):
    import types

    if "ddgs" not in sys.modules:
        monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=object))
    mcp_dir = str(Path(__file__).resolve().parents[2] / "services" / "mcps" / "research")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import server as mcp_server

    assert "binary_contract_valuation" in mcp_server.TOOLS
    schema = mcp_server.TOOLS["describe_binary_contract_valuation"]({})
    assert schema["read_only"] is True
    from app.services.intelligence.valuation_evidence import RecordedValuationRequest

    assert schema["input_schema"] == RecordedValuationRequest.model_json_schema()
