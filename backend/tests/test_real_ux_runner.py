"""Credentialed test setup copies only provider keys and regenerates user authentication."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[2] / "scripts/run-real-ux.py"
    spec = importlib.util.spec_from_file_location("real_ux_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_env_file_does_not_copy_production_credentials(runner, tmp_path, monkeypatch):
    for name in runner.PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UX_TEST_DATABASE_URL", "postgresql:///explicit_test_database")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "parent-wallet-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "parent-github-secret")
    monkeypatch.setenv("JWT_SECRET_KEY", "parent-auth-secret")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=test-openai\nTAVILY_API_KEY=test-tavily\nNEWSAPI_API_KEY=test-news\n"
        "POLYMARKET_PRIVATE_KEY=production-wallet\nGITHUB_TOKEN=production-github\n"
        "JWT_SECRET_KEY=production-jwt\nDATABASE_URL=postgresql:///production\n"
    )
    first = runner.child_environment(env_file, "test_schema", tmp_path)
    second = runner.child_environment(env_file, "test_schema", tmp_path)
    assert first["OPENAI_API_KEY"] == "test-openai"
    assert first["TAVILY_API_KEY"] == "test-tavily"
    assert first["NEWSAPI_API_KEY"] == "test-news"
    assert "POLYMARKET_PRIVATE_KEY" not in first
    assert "GITHUB_TOKEN" not in first
    assert first["DATABASE_URL"] == "postgresql:///explicit_test_database"
    assert first["JWT_SECRET_KEY"] != second["JWT_SECRET_KEY"]
    assert first["JWT_SECRET_KEY"] not in {"parent-auth-secret", "production-jwt"}
    assert first["PGOPTIONS"] == "-csearch_path=test_schema"
    assert first["LLM_TIERING_ENABLED"] == "false"
    assert first["LIVE_TRADING_ENABLED"] == "false"


def test_missing_provider_key_cannot_silently_skip_live_tests(runner, tmp_path, monkeypatch):
    for name in runner.PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UX_TEST_DATABASE_URL", "postgresql:///test")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        runner.child_environment(None, "test_schema", tmp_path)


def test_test_database_must_be_explicit(runner, tmp_path, monkeypatch):
    for name in runner.PROVIDER_KEYS:
        monkeypatch.setenv(name, "test-provider-key")
    monkeypatch.delenv("UX_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql:///production")
    with pytest.raises(ValueError, match="UX_TEST_DATABASE_URL"):
        runner.child_environment(None, "test_schema", tmp_path)
