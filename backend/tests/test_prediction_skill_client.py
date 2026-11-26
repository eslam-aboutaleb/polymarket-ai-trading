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
        ("maker_economics", "maker-economics-v1"),
        ("event_correlation_risk", "event-correlation-risk-v1"),
        ("recorded_event_correlation_risk", "event-correlation-risk-v1"),
        ("settlement_and_redemption", "settlement-lifecycle-v1"),
        ("recorded_settlement_and_redemption", "settlement-lifecycle-v1"),
        ("forecast_calibration", "forecast-skill-report-v1"),
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


def _load_research_server():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "services" / "mcps" / "research" / "server.py"
    import sys
    import types

    # The web-search library is unrelated to this bridge and absent from the backend env.
    if "ddgs" not in sys.modules:
        try:
            import ddgs  # noqa: F401
        except ImportError:
            sys.modules["ddgs"] = types.SimpleNamespace(DDGS=object)
    spec = importlib.util.spec_from_file_location("research_server_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_maker_economics_bridge_sends_fixed_path_and_validated_params_only():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("X-Account-Id")))
            body = json.dumps(
                {"schema_version": "maker-economics-v1", "execution_eligible": False, "ok": 1}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    module = _load_research_server()
    module.VENUE_API_BASE = f"http://127.0.0.1:{server.server_port}"
    try:
        call = module._structured_skill_research
        ok = asyncio.run(call("maker_economics", {"config_id": 7, "account_id": 3}))
        assert ok["ok"] == 1 and seen == [("/api/market-maker/economics?config_id=7", "3")]
        for bad in (
            {},
            {"config_id": True},
            {"config_id": 0},
            {"config_id": "7"},
            {"config_id": 7, "path": "/api/other"},
        ):
            result = asyncio.run(call("maker_economics", bad))
            assert result["reason"] == "READ_ONLY_SKILL_UNAVAILABLE"
        assert len(seen) == 1  # nothing invalid reached the backend
        tool = module.TOOLS["describe_maker_economics"]({})
        assert tool["read_only"] is True and tool["version"] == "maker-economics-v1"
    finally:
        server.shutdown()
