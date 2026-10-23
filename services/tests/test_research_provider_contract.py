"""Real gRPC/backend/provider prompting with only the external model/search mocked.

Each provider runs in a child interpreter because both services own a package
named src and a generated analysis_pb2 module. This also catches import/runtime
compatibility without making test ordering decide which provider gets loaded.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
import asyncio
import importlib
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("JWT_SECRET_KEY", "provider-contract-disposable-signing-secret")
os.environ.setdefault("REFRESH_TOKEN_HASH_SECRET", "provider-contract-disposable-refresh-secret")
os.environ.setdefault("ENVIRONMENT", "test")

# Reflection is optional test-server infrastructure, not the RPC being tested.
try:
    import grpc_reflection.v1alpha.reflection
except ImportError:
    root = types.ModuleType("grpc_reflection")
    alpha = types.ModuleType("grpc_reflection.v1alpha")
    alpha.reflection = SimpleNamespace(
        SERVICE_NAME="reflection", enable_server_reflection=lambda *a: None)
    sys.modules["grpc_reflection"] = root
    sys.modules["grpc_reflection.v1alpha"] = alpha

import grpc
import analysis_pb2_grpc
from app.config import AIBackend
from app.grpc_clients.analysis_client import AnalysisClient
from polymarket_mcp.research_contract import (
    RESEARCH_CONTRACT, RESEARCH_SYSTEM_PROMPT, structured_research_prompt)

for invalid in ("", "x" * 64001):
    try:
        structured_research_prompt(invalid, "source")
    except ValueError:
        pass
    else:
        raise AssertionError("Instruction size boundary was not enforced")
assert "tail-must-not-appear" not in structured_research_prompt(
    "x" * 64000, "e" * 100000 + "tail-must-not-appear")

kind = sys.argv[1]
server_module = importlib.import_module("src.server")
module = importlib.import_module("src.analysis_chain" if kind == "llm-chain" else "src.cli_agent")
engine_type = module.TradingAnalysisChain if kind == "llm-chain" else module.CopilotCLIAgent
engine = object.__new__(engine_type)
engine.prompts = SimpleNamespace(get=lambda category, name:
    "LEGACY_PROMPT {market_description}" if name == "market_assessment" else "LEGACY_SYSTEM")
research = "• Official report\n  The official tally is not yet certified.\n  Source: https://evidence.example/report"
output = '{"summary":"Research only","claims":[],"missing_information":[]}'
model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=output)))
if kind == "llm-chain":
    engine._get_task_llm = Mock(return_value=model)
    engine._research_market = AsyncMock(return_value=research)
    server_module._get_chain_for_request = lambda request: engine
else:
    engine.research_mcp = SimpleNamespace(
        gather_market_research=AsyncMock(return_value={"web_summary": research}))
    engine._run_analysis = AsyncMock(return_value=output)
    engine._llm_config_for_task = Mock(return_value=None)

servicer = object.__new__(server_module.AnalysisServicer)
servicer.settings = SimpleNamespace(llm_model="fixture")
servicer.agent = engine

async def run():
    server = grpc.aio.server()
    analysis_pb2_grpc.add_AnalysisServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    client = AnalysisClient(backend=AIBackend.LLM_CHAIN)
    client._get_stub = AsyncMock(return_value=analysis_pb2_grpc.AnalysisServiceStub(channel))
    args = dict(market_title="Will the measure pass?", market_description="Official tally rule.",
                yes_price=.7, no_price=.3, volume_24h=0, end_date="2026-11-03",
                include_research=True)
    try:
        result = await client.analyze_market(
            **args, research_instructions="Return only the bounded research JSON schema.")
        assert result["metadata"]["research_contract"] == RESEARCH_CONTRACT
        assert result["research_context"] == research
        assert result["analysis"] == output
        if kind == "llm-chain":
            messages = model.ainvoke.call_args.args[0]
            assert messages[0].content == RESEARCH_SYSTEM_PROMPT
            prompt = messages[1].content
            engine._research_market.assert_awaited_once_with(
                args["market_title"], args["market_description"])
        else:
            prompt = engine._run_analysis.call_args.args[0]
            assert engine._run_analysis.call_args.kwargs["system_prompt"] == RESEARCH_SYSTEM_PROMPT
            assert engine.research_mcp.gather_market_research.call_args.kwargs[
                "market_title"] == args["market_title"]
        assert "Return only the bounded research JSON schema." in prompt
        assert "LEGACY_PROMPT" not in prompt and "LEGACY_SYSTEM" not in prompt
        assert "untrusted_research_context" in prompt and "not yet certified" in prompt
        legacy = await client.analyze_market(**args)
        assert "research_contract" not in legacy["metadata"]
        if kind == "llm-chain":
            assert model.ainvoke.call_args.args[0][0].content == "LEGACY_SYSTEM"
        else:
            assert engine._run_analysis.call_args.kwargs["system_prompt"] == "LEGACY_SYSTEM"
    finally:
        await client.close()
        await channel.close()
        await server.stop(0)

asyncio.run(run())
"""


@pytest.mark.parametrize("provider", ["llm-chain", "cli-agent"])
def test_real_rpc_preserves_explicit_research_contract_and_legacy_prompts(provider):
    service = ROOT / "services" / provider
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            map(
                str,
                [
                    service,
                    service / "src",
                    ROOT / "services/shared",
                    ROOT / "backend",
                ],
            )
        ),
    }
    result = subprocess.run(  # noqa: S603 - fixed interpreter/script and allowlisted provider
        [sys.executable, "-c", SCRIPT, provider],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
