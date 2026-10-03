"""Tier routing and provider-aware model selection tests.

The tier defaults are OpenAI models. These tests pin the
behaviour that keeps an OpenAI model name from being sent to
another provider when a request selects that provider.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from polymarket_settings import LLMProvider, SharedLLMSettings

LLM_CHAIN_SRC = Path(__file__).resolve().parents[2] / "services" / "llm-chain"


@pytest.fixture(scope="module")
def llm_factory():
    """Import ``src.llm_factory`` with the llm-chain source on ``sys.path``."""
    sys.path.insert(0, str(LLM_CHAIN_SRC))
    try:
        from src import llm_factory

        yield llm_factory
    finally:
        sys.path.remove(str(LLM_CHAIN_SRC))
        for module_name in ("src.llm_factory", "src.config", "src"):
            sys.modules.pop(module_name, None)


@pytest.fixture()
def settings() -> SharedLLMSettings:
    return SharedLLMSettings(_env_file=None)


def _record(llm_factory, monkeypatch, settings, provider: LLMProvider) -> dict:
    calls: dict = {}

    def fake_create(settings, kwargs, model_override=None, reasoning_effort=None):
        calls["settings"] = settings
        calls["kwargs"] = kwargs
        calls["model"] = model_override
        calls["reasoning"] = reasoning_effort
        return f"{provider.value}-llm"

    attr = f"_create_{provider.value}_llm"
    monkeypatch.setattr(llm_factory, attr, fake_create)
    monkeypatch.setattr(llm_factory, "get_settings", lambda: settings)
    return calls


def test_openai_task_uses_tier_defaults(llm_factory, monkeypatch, settings) -> None:
    calls = _record(llm_factory, monkeypatch, settings, LLMProvider.OPENAI)

    result = llm_factory.create_llm_for_task("quick_analysis")

    assert result == "openai-llm"
    assert calls["model"] == "gpt-4o-mini"
    assert calls["kwargs"]["temperature"] == 0.0
    assert calls["kwargs"]["max_tokens"] == 512
    assert calls["reasoning"] is None


def test_strong_openai_task_applies_reasoning_effort(llm_factory, monkeypatch, settings) -> None:
    calls = _record(llm_factory, monkeypatch, settings, LLMProvider.OPENAI)

    llm_factory.create_llm_for_task("market_assessment")

    assert calls["model"] == "gpt-4o"
    assert calls["kwargs"]["temperature"] == 0.2
    assert calls["kwargs"]["max_tokens"] == 2048
    assert calls["reasoning"] == "medium"


def test_explicit_model_beats_the_tier_default(llm_factory, monkeypatch, settings) -> None:
    calls = _record(llm_factory, monkeypatch, settings, LLMProvider.OPENAI)

    llm_factory.create_llm_for_task(
        "market_assessment",
        llm_override=llm_factory.LLMRequestConfig(provider=LLMProvider.OPENAI, model="gpt-4.1"),
    )

    assert calls["model"] == "gpt-4.1"


def test_cross_provider_override_uses_the_provider_catalog(
    llm_factory, monkeypatch, settings
) -> None:
    calls = _record(llm_factory, monkeypatch, settings, LLMProvider.ANTHROPIC)

    llm_factory.create_llm_for_task(
        "market_assessment",
        llm_override=llm_factory.LLMRequestConfig(provider=LLMProvider.ANTHROPIC),
    )

    assert calls["model"] == "claude-sonnet-4"


def test_non_openai_service_default_uses_the_provider_catalog(
    llm_factory, monkeypatch, settings
) -> None:
    settings.llm_provider = LLMProvider.GOOGLE
    calls = _record(llm_factory, monkeypatch, settings, LLMProvider.GOOGLE)

    llm_factory.create_llm_for_task("market_assessment")

    assert calls["model"] == "gemini-2.5-flash"


def test_request_override_uses_the_provider_catalog(llm_factory, monkeypatch, settings) -> None:
    calls = _record(llm_factory, monkeypatch, settings, LLMProvider.GROQ)

    llm_factory.create_llm_for_request(llm_factory.LLMRequestConfig(provider=LLMProvider.GROQ))

    assert calls["model"] == "llama-3.3-70b-versatile"
