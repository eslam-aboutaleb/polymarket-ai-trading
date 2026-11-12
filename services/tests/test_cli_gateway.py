"""Exercise CLI transport with native credentials present and a mocked gateway."""

import os
import subprocess
import sys
from pathlib import Path


def test_cli_agent_uses_gateway_and_never_falls_back():
    root = Path(__file__).resolve().parents[2]
    script = r"""
import asyncio
import httpx
import sys
import types
from pathlib import Path
package = types.ModuleType("src")
package.__path__ = [str(Path.cwd() / "services/cli-agent/src")]
sys.modules["src"] = package
from pydantic import SecretStr
from src import cli_agent
from src.config import Settings

settings = Settings(
    _env_file=None,
    gateway_base="https://gateway.example/v1/",
    gateway_key="gateway-key",
    gateway_model="custom-chat",
    github_token="native-github-key",
    openai_api_key="native-openai-key",
    llm_tier_strong_model="custom-strong",
)
cli_agent.get_settings = lambda: settings
requests = []

def respond(request):
    requests.append(request)
    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

async def run():
    agent = cli_agent.CopilotCLIAgent()
    await agent._client.aclose()
    agent._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert agent._llm_config_for_task("market_assessment").model == "custom-strong"
        assert agent._llm_config_for_task("quick_analysis").model == "custom-chat"
        assert await agent._call_llm("Synthetic input") == "OK"
        assert str(requests[0].url) == "https://gateway.example/v1/chat/completions"
        assert requests[0].headers["Authorization"] == "Bearer gateway-key"
    finally:
        await agent.aclose()
    settings.gateway_key = SecretStr("")
    try:
        cli_agent.CopilotCLIAgent()
    except ValueError as exc:
        assert "LITELLM_API_BASE and LITELLM_API_KEY" in str(exc)
    else:
        raise AssertionError("Native credentials bypassed gateway requirements")

asyncio.run(run())
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "LITELLM_"))}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "services/shared"), str(root / "services/cli-agent")]
    )
    subprocess.run(  # noqa: S603 - fixed script, no shell or user input
        [sys.executable, "-c", script], env=env, cwd=root, check=True, timeout=30
    )
