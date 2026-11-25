"""Issue #22: the read-only research tool registration and its argument guard."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

SERVER = Path(__file__).parents[2] / "services/mcps/research/server.py"


@pytest.fixture(scope="module")
def server():
    if importlib.util.find_spec("ddgs") is None:
        # The server only needs the search client at call time of unrelated tools.
        sys.modules["ddgs"] = types.SimpleNamespace(DDGS=object)
    sys.path.insert(0, str(SERVER.parent))
    spec = importlib.util.spec_from_file_location("research_server_forecast", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_is_registered_read_only_and_described(server):
    schema = server.TOOLS["describe_forecast_calibration"]({})
    assert schema["read_only"] is True
    assert schema["version"] == "forecast-skill-report-v1"
    assert schema["input_schema"]["additionalProperties"] is False
    assert "forecast_calibration" in server.TOOLS
    assert not any("order" in name for name in server.TOOLS if name.startswith("forecast"))


@pytest.mark.parametrize(
    "args",
    [
        {"window_days": 0},
        {"window_days": 366},
        {"window_days": True},
        {"x": 1},
        {"window_days": "7"},
    ],
)
def test_bad_arguments_abstain_without_any_request(server, args, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no network call may be made for invalid arguments")

    monkeypatch.setattr(server.httpx, "AsyncClient", boom)
    result = asyncio.run(server._structured_skill_research("forecast_calibration", args))
    assert result["status"] == "abstain"
    assert result["reason"] == "READ_ONLY_SKILL_UNAVAILABLE"
    assert result["execution_eligible"] is False
