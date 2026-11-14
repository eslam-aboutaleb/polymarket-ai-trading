"""Unit, scenario, API, and MCP tests for event-correlation-risk (#21)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.main import app as fastapi_app
from app.services.analytics.event_correlation_risk import (
    EventCorrelationRequest,
    ExposureLeg,
    LogicalRelation,
    assess_event_correlation_risk,
)

CID1 = "0x" + "1" * 64
CID2 = "0x" + "2" * 64
CID3 = "0x" + "3" * 64
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_correlated_btc_up_contracts_and_pending_orders_aggregate():
    req = EventCorrelationRequest(
        legs=[
            ExposureLeg(
                condition_id=CID1,
                token_id="101",
                outcome="YES",
                shares=Decimal("100"),
                entry_price=Decimal("0.60"),
                status="open_position",
                underlying_asset="BTC",
            ),
            ExposureLeg(
                condition_id=CID2,
                token_id="202",
                outcome="YES",
                shares=Decimal("100"),
                entry_price=Decimal("0.40"),
                status="pending_order",
                underlying_asset="BTC",
            ),
        ],
        as_of=NOW,
    )
    res = assess_event_correlation_risk(req)
    assert res.status == "assessed"
    assert res.decision == "advisory_only"
    assert res.execution_eligible is False
    assert res.total_gross_notional == Decimal("100.00")
    assert res.total_pending_notional == Decimal("40.00")
    assert res.worst_case_portfolio_loss == Decimal("100.00")
    assert res.cash_tied_until_settlement == Decimal("100.00")
    assert len(res.clusters) == 3
    assert any(cluster.cluster_key == "asset:BTC" for cluster in res.clusters)
    assert "AGGREGATED_CORRELATED_CONTRACTS:asset:BTC" in res.concentration_warnings
    assert "CLUSTER_CONCENTRATION_EXCEEDED:asset:BTC" in res.concentration_warnings


def test_complementary_yes_no_hedge_does_not_double_count_loss():
    # 100 YES @ 0.55 + 100 NO @ 0.40 on same condition -> gross 95, guaranteed 100, worst loss 0.
    req = EventCorrelationRequest(
        legs=[
            ExposureLeg(
                condition_id=CID1,
                token_id="101",
                outcome="YES",
                shares=Decimal("100"),
                entry_price=Decimal("0.55"),
                event_id="evt-1",
            ),
            ExposureLeg(
                condition_id=CID1,
                token_id="102",
                outcome="NO",
                shares=Decimal("100"),
                entry_price=Decimal("0.40"),
                event_id="evt-1",
            ),
        ],
        as_of=NOW,
    )
    res = assess_event_correlation_risk(req)
    assert res.total_gross_notional == Decimal("95.00")
    assert res.worst_case_portfolio_loss == Decimal("0")
    assert res.clusters[0].net_hedged_offset == Decimal("95.00")


def test_mutually_exclusive_basket_enumerates_joint_states_and_marginal_loss():
    # Buy 100 YES @ 0.40 on CID1 and 100 YES @ 0.35 on CID2 (mutually exclusive).
    # If CID1 wins: +60 - 35 = +25. If CID2 wins: -40 + 65 = +25. If neither wins: -75.
    # Adding proposed CID3 YES @ 0.20 (also mutually exclusive): worst = 95, marginal = +20.
    req = EventCorrelationRequest(
        legs=[
            ExposureLeg(
                condition_id=CID1,
                token_id="101",
                outcome="YES",
                shares=Decimal("100"),
                entry_price=Decimal("0.40"),
                event_id="election",
            ),
            ExposureLeg(
                condition_id=CID2,
                token_id="201",
                outcome="YES",
                shares=Decimal("100"),
                entry_price=Decimal("0.35"),
                event_id="election",
            ),
        ],
        relations=[
            LogicalRelation(
                relation_type="mutually_exclusive",
                left_condition_id=CID1,
                right_condition_id=CID2,
            ),
            LogicalRelation(
                relation_type="mutually_exclusive",
                left_condition_id=CID1,
                right_condition_id=CID3,
            ),
            LogicalRelation(
                relation_type="mutually_exclusive",
                left_condition_id=CID2,
                right_condition_id=CID3,
            ),
        ],
        proposed_leg=ExposureLeg(
            condition_id=CID3,
            token_id="301",
            outcome="YES",
            shares=Decimal("100"),
            entry_price=Decimal("0.20"),
            event_id="election",
        ),
        as_of=NOW,
    )
    res = assess_event_correlation_risk(req)
    assert res.worst_case_portfolio_loss == Decimal("95.00")
    assert res.proposed_marginal_worst_loss == Decimal("20.00")


def test_event_correlation_api_and_mcp_tool(monkeypatch):
    import types

    fastapi_app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    client = TestClient(fastapi_app)
    get_settings.cache_clear()
    try:
        assert client.get("/api/risk/event-correlation/schema").status_code == 503
        monkeypatch.setenv("EVENT_CORRELATION_RISK_ENABLED", "true")
        get_settings.cache_clear()
        assert client.get("/api/risk/event-correlation/schema").status_code == 200
        resp = client.post(
            "/api/risk/event-correlation/analyze",
            json={
                "legs": [
                    {
                        "condition_id": CID1,
                        "token_id": "101",
                        "outcome": "YES",
                        "shares": "100",
                        "entry_price": "0.60",
                        "underlying_asset": "BTC",
                    }
                ],
                "as_of": NOW.isoformat(),
            },
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["worst_case_portfolio_loss"]) == Decimal("60.00")
        assert resp.json()["execution_eligible"] is False
    finally:
        fastapi_app.dependency_overrides.clear()
        get_settings.cache_clear()

    if "ddgs" not in sys.modules:
        monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=object))
    mcp_dir = str(Path(__file__).resolve().parents[2] / "services" / "mcps" / "research")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import server as mcp_server

    assert "event_correlation_risk" in mcp_server.TOOLS
    assert mcp_server.TOOLS["describe_event_correlation_risk"]({})["read_only"] is True
