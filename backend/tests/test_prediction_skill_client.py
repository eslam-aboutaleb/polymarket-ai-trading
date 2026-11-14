"""Research/Analysis/orchestrator entry cannot invoke execution or accept unsafe output."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from polymarket_mcp import ResearchMCPClient


@pytest.mark.parametrize(
    "tool,version",
    [
        ("resolution_rule_analysis", "resolution-rule-v1"),
        ("binary_contract_valuation", "binary-valuation-v1"),
        ("oracle_basis_risk", "oracle-basis-v1"),
        ("event_correlation_risk", "event-correlation-risk-v1"),
        ("recorded_event_correlation_risk", "event-correlation-risk-v1"),
        ("settlement_and_redemption", "settlement-lifecycle-v1"),
        ("recorded_settlement_and_redemption", "settlement-lifecycle-v1"),
    ],
)
def test_structured_skill_result_must_be_known_version_and_read_only(tool, version):
    client = ResearchMCPClient()
    client._call_tool = AsyncMock(
        return_value={"schema_version": version, "execution_eligible": False, "status": "abstain"}
    )
    assert asyncio.run(client.prediction_market_skill(tool, {}))["status"] == "abstain"
    client._call_tool = AsyncMock(
        return_value={"schema_version": version, "execution_eligible": True}
    )
    assert (
        asyncio.run(client.prediction_market_skill(tool, {}))["reason"]
        == "SKILL_TOOL_UNAVAILABLE_OR_UNSAFE"
    )
    with pytest.raises(ValueError, match="read-only"):
        asyncio.run(client.prediction_market_skill("submit_order", {}))
