"""Run bounded, credentialed user flows against isolated API/DB/LLM processes.

Only the three explicitly required provider credentials may be read from a local
.env. Production wallet, JWT, GitHub and database credentials are never copied.
CI supplies these keys only on trusted push/dispatch/schedule runs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import grpc
from dotenv import dotenv_values
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KEYS = ("OPENAI_API_KEY", "TAVILY_API_KEY", "NEWSAPI_API_KEY")
ADMIN_WALLET = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"


def child_environment(env_file: Path | None, schema: str, runtime: Path) -> dict[str, str]:
    credentials = dotenv_values(env_file) if env_file else {}
    env = dict(os.environ)
    # A parent shell can contain production keys. Only the allow-listed test
    # providers are forwarded; all app authentication/funding is disposable.
    for key in (
        "POLYMARKET_PRIVATE_KEY",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
    ):
        env.pop(key, None)
    for key in PROVIDER_KEYS:
        value = os.environ.get(key) or credentials.get(key)
        if not value or value.startswith(("your-", "your_")):
            raise ValueError(f"Required real UX credential is missing: {key}")
        env[key] = value
    test_database = os.environ.get("UX_TEST_DATABASE_URL")
    if not test_database or not test_database.startswith(
        ("postgresql://", "postgresql+psycopg2://")
    ):
        raise ValueError("UX_TEST_DATABASE_URL must explicitly identify a PostgreSQL test database")
    env.update(
        DATABASE_URL=test_database,
        PGOPTIONS=f"-csearch_path={schema}",
        ENVIRONMENT="development",
        ROLE="web",
        REDIS_URL="",
        JWT_SECRET_KEY=secrets.token_hex(32),
        REFRESH_TOKEN_HASH_SECRET=secrets.token_hex(32),
        REQUIRE_REDIS_FOR_CREDENTIALS="false",
        REQUIRE_REDIS_FOR_CHALLENGES="false",
        RATE_LIMIT_PER_MINUTE="1000",
        ADMIN_WALLETS=ADMIN_WALLET,
        VITE_API_URL="",
        E2E_BASE_URL="http://localhost:5174",
        CORS_ALLOWED_ORIGINS="http://localhost:5174,http://localhost:5173,http://localhost:3000",
        DEFAULT_AI_BACKEND="llm_chain",
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-4o-mini",
        LLM_TIERING_ENABLED="false",
        LLM_MAX_TOKENS="1536",
        LLM_TEMPERATURE="0",
        LLM_CHAIN_GRPC_HOST="127.0.0.1",
        LLM_CHAIN_GRPC_PORT="59151",
        GRPC_PORT="59151",
        CLI_AGENT_GRPC_HOST="127.0.0.1",
        CLI_AGENT_GRPC_PORT="59152",
        GRPC_TIMEOUT="150",
        TAVILY_ENABLED="true",
        NEWSAPI_ENABLED="true",
        RAG_ENABLED="false",
        BINANCE_SKILLS_ENABLED="false",
        LIVE_TRADING_ENABLED="false",
        PROMPTS_PATH=str(ROOT / "prompts/prompts.json"),
        LOG_LEVEL="WARNING",
        PYTHONPATH=os.pathsep.join(
            (
                str(runtime),
                str(ROOT / "services/shared"),
                str(ROOT / "services/llm-chain"),
                str(ROOT / "backend"),
            )
        ),
    )
    return env


async def wait_for_llm(runtime: Path) -> None:
    sys.path.insert(0, str(runtime))
    import analysis_pb2
    import analysis_pb2_grpc

    async with grpc.aio.insecure_channel("127.0.0.1:59151") as channel:
        await asyncio.wait_for(channel.channel_ready(), timeout=60)
        response = await analysis_pb2_grpc.AnalysisServiceStub(channel).HealthCheck(
            analysis_pb2.HealthRequest(), timeout=10
        )
        if not response.healthy or response.service_name != "llm-chain-service":
            raise RuntimeError("Expected LLM service is not healthy")
        if (
            response.capabilities.get("tavily") != "true"
            or response.capabilities.get("newsapi") != "true"
        ):
            raise RuntimeError("Research providers were not enabled")


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path, help="Read only the three provider keys from this file"
    )
    args = parser.parse_args()
    schema = "real_ux_" + uuid4().hex
    service_python = ROOT / "services/llm-chain/.venv/bin/python"
    if not service_python.is_file():
        raise ValueError(
            "Install the locked LLM service first with uv sync "
            "--project services/llm-chain --frozen --python 3.11"
        )
    output = ROOT / "frontend/real-ux-artifacts/services.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    service = None
    browser = None
    admin = None
    schema_created = False
    with tempfile.TemporaryDirectory(prefix="real-ux-proto-") as directory:
        runtime = Path(directory)
        env = child_environment(args.env_file, schema, runtime)
        # Never let another developer's stack satisfy the readiness test.
        import socket

        for port in (8002, 5174, 59151):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise ValueError(f"Port {port} is already in use; stop that test stack first")
        try:
            with output.open("w") as log:

                def run(command: list[str], cwd: Path) -> None:
                    subprocess.run(  # noqa: S603 - fixed argv, no shell or user-supplied commands
                        command,
                        cwd=cwd,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=180,
                    )

                # The service lock can constrain protobuf below the API runtime.
                # Generate with its compiler so its own server can load the schema.
                run(
                    [
                        str(service_python),
                        "-m",
                        "grpc_tools.protoc",
                        "-I",
                        "services/proto",
                        f"--python_out={runtime}",
                        f"--grpc_python_out={runtime}",
                        "services/proto/analysis.proto",
                    ],
                    ROOT,
                )
                admin = create_engine(env["DATABASE_URL"])
                with admin.begin() as connection:
                    connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                schema_created = True
                run([sys.executable, "-m", "alembic", "upgrade", "head"], ROOT / "backend")
                run(
                    [
                        sys.executable,
                        "-c",
                        f'''from app.models.users.user import User
from app.utils.database import SessionLocal
with SessionLocal.begin() as db:
    db.add(User(wallet_address="{ADMIN_WALLET}", is_admin=True))
''',
                    ],
                    ROOT / "backend",
                )
                service = subprocess.Popen(  # noqa: S603 - fixed service entrypoint
                    [str(service_python), "-m", "src.server"],
                    cwd=ROOT / "services/llm-chain",
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                asyncio.run(wait_for_llm(runtime))
                logging.info("LLM and research capabilities ready; running three real user flows")
                npm = shutil.which("npm")
                if npm is None:
                    raise ValueError("npm is required")
                browser = subprocess.Popen(  # noqa: S603 - fixed browser test command
                    [
                        npm,
                        "exec",
                        "--",
                        "playwright",
                        "test",
                        "--config",
                        "playwright.real.config.ts",
                    ],
                    cwd=ROOT / "frontend",
                    env=env,
                    start_new_session=True,
                )
                return browser.wait(timeout=600)
        finally:
            stop_process(browser)
            stop_process(service)
            if admin is not None:
                if schema_created:
                    with admin.begin() as connection:
                        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                admin.dispose()
            if output.exists():
                redacted = output.read_text(errors="replace")
                for key in PROVIDER_KEYS:
                    redacted = redacted.replace(env[key], "[REDACTED]")
                output.write_text(redacted)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
        # No exception body: SDK/subprocess errors can contain credentials.
        logging.error("Real UX failed (%s); inspect the redacted service log", type(error).__name__)
        raise SystemExit(1) from None
