"""Unit tests for the shared LLM settings and tier resolver."""

from __future__ import annotations

import os

import pytest
from polymarket_settings import (
    MODEL_CATALOG,
    LLMProvider,
    ModelTier,
    SharedLLMSettings,
    resolve_tier_config,
)


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch):
    """Isolate these tests from developer LLM_* environment variables."""
    for key in list(os.environ):
        if key.startswith("LLM_"):
            monkeypatch.delenv(key)


def _settings() -> SharedLLMSettings:
    return SharedLLMSettings(_env_file=None)


def test_default_model_is_not_the_retired_gpt_4_turbo_preview():
    settings = _settings()
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_model != "gpt-4-turbo-preview"


def test_env_overrides_replace_the_global_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gpt-4.1")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")
    monkeypatch.setenv("LLM_MAX_TOKENS", "1234")
    monkeypatch.setenv("LLM_TIERING_ENABLED", "false")

    settings = _settings()

    assert settings.llm_model == "gpt-4.1"
    assert settings.llm_temperature == pytest.approx(0.7)
    assert settings.llm_max_tokens == 1234
    assert settings.llm_tiering_enabled is False


def test_quick_analysis_resolves_to_the_cheap_tier():
    model, temperature, max_tokens, reasoning = resolve_tier_config("quick_analysis", _settings())

    assert model == "gpt-4o-mini"
    assert temperature == pytest.approx(0.0)
    assert max_tokens == 512
    assert reasoning is None


def test_market_assessment_resolves_to_the_strong_tier():
    model, temperature, max_tokens, reasoning = resolve_tier_config(
        "market_assessment", _settings()
    )

    assert model == "gpt-4o"
    assert temperature == pytest.approx(0.2)
    assert max_tokens == 2048
    assert reasoning == "medium"


def test_news_article_resolves_to_the_writer_tier():
    model, temperature, max_tokens, reasoning = resolve_tier_config("news_article", _settings())

    assert model == "gpt-4o"
    assert temperature == pytest.approx(0.6)
    assert max_tokens == 4096
    assert reasoning is None


def test_unknown_task_falls_back_to_the_standard_tier():
    model, temperature, max_tokens, reasoning = resolve_tier_config("not_a_real_task", _settings())

    assert model == "gpt-4o-mini"
    assert temperature == pytest.approx(0.2)
    assert max_tokens == 2048
    assert reasoning is None


def test_tiering_disabled_returns_the_global_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gpt-4.1")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.9")
    monkeypatch.setenv("LLM_MAX_TOKENS", "999")
    monkeypatch.setenv("LLM_TIERING_ENABLED", "false")

    model, temperature, max_tokens, reasoning = resolve_tier_config(
        "market_assessment", _settings()
    )

    assert model == "gpt-4.1"
    assert temperature == pytest.approx(0.9)
    assert max_tokens == 999
    assert reasoning is None


def test_per_tier_overrides_win_over_tier_defaults(monkeypatch):
    monkeypatch.setenv("LLM_TIER_CHEAP_MODEL", "gpt-4.1-nano")
    monkeypatch.setenv("LLM_TIER_CHEAP_TEMPERATURE", "0.1")
    monkeypatch.setenv("LLM_TIER_CHEAP_MAX_TOKENS", "256")
    monkeypatch.setenv("LLM_STRONG_REASONING_EFFORT", "high")

    settings = _settings()

    cheap = resolve_tier_config("quick_analysis", settings)
    assert cheap == ("gpt-4.1-nano", 0.1, 256, None)

    strong = resolve_tier_config("market_assessment", settings)
    assert strong[0] == "gpt-4o"
    assert strong[3] == "high"


def test_model_catalog_contains_no_retired_models():
    stale = {
        "gpt-4-turbo-preview",
        "gpt-3.5-turbo",
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    }
    advertised = {model for models in MODEL_CATALOG.values() for model in models}
    assert not advertised & stale


def test_model_catalog_covers_every_provider():
    assert set(MODEL_CATALOG) == set(LLMProvider)
    assert all(MODEL_CATALOG[provider] for provider in LLMProvider)
    assert ModelTier.CHEAP in set(ModelTier)
