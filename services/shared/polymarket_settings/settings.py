"""Centralized LLM provider/tier definitions and shared settings.

This module is the single source of truth for:

* ``LLMProvider`` — the canonical provider enum (replaces the three
  drifted copies in ``llm-chain``, the backend gateway and the user
  settings model).
* ``ModelTier`` / ``TASK_TIERS`` — per-task model routing. Cheap
  classification work runs on small fast models; forecasting and
  trading decisions run on the strong tier; long-form writing runs
  on the writer tier.
* ``MODEL_CATALOG`` — current-generation models per provider (the
  stale ``gpt-4-turbo-preview`` / ``claude-3-*`` / ``gemini-1.5-*``
  suggestions are gone).
* ``SharedLLMSettings`` — the pydantic-settings base every service
  config inherits from.
* ``resolve_tier_config`` — tier lookup with per-tier env overrides.

Tier map
--------
CHEAP     (gpt-4o-mini, t=0.0,  512 tokens)  classification / extraction / paraphrase
STANDARD  (gpt-4o-mini, t=0.2, 2048 tokens)  everyday analysis
STRONG    (gpt-4o, t=0.2, 2048 tokens, reasoning=medium) forecasting / trading decisions
WRITER    (gpt-4o,      t=0.6, 4096 tokens)  long-form news articles
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    """Supported LLM providers (canonical, shared definition).

    ``llm-chain`` handles OPENAI/ANTHROPIC/GOOGLE/GROQ/OLLAMA;
    ``cli-agent`` handles GITHUB_MODELS.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    GROQ = "groq"
    OLLAMA = "ollama"  # Local models
    GITHUB_MODELS = "github_models"


class ModelTier(StrEnum):
    """Model tiers for per-task routing."""

    CHEAP = "cheap"
    STANDARD = "standard"
    STRONG = "strong"
    WRITER = "writer"


#: Current-generation models per provider. Replaces the stale
#: ``MODEL_SUGGESTIONS`` dicts that still advertised retired models
#: (``gpt-4-turbo-preview``, ``claude-3-*``, ``gemini-1.5-*``).
MODEL_CATALOG: dict[LLMProvider, list[str]] = {
    LLMProvider.OPENAI: [
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "gpt-4o",
        "gpt-4o-mini",
    ],
    LLMProvider.ANTHROPIC: [
        "claude-sonnet-4",
        "claude-haiku-4-5",
        "claude-opus-4",
    ],
    LLMProvider.GOOGLE: [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
    ],
    LLMProvider.GROQ: [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
    ],
    LLMProvider.OLLAMA: [
        "llama3.2",
        "llama3.1",
        "mistral",
        "codellama",
        "phi3",
    ],
    LLMProvider.GITHUB_MODELS: [
        "gpt-4o",
        "gpt-4o-mini",
    ],
}

#: Maps a task name (the prompt-store key / analysis method) to the
#: model tier that should run it. Unknown tasks fall back to
#: ``ModelTier.STANDARD``.
TASK_TIERS: dict[str, ModelTier] = {
    # ── CHEAP: classification, extraction, paraphrase ──────────
    "quick_analysis": ModelTier.CHEAP,
    "multi_market_scan": ModelTier.CHEAP,
    "filter_events": ModelTier.CHEAP,
    "filter_markets": ModelTier.CHEAP,
    "sentiment_analysis": ModelTier.CHEAP,
    "easy_trade_scoring": ModelTier.CHEAP,
    "event_quick_analysis": ModelTier.CHEAP,
    "multi_query_expansion": ModelTier.CHEAP,
    "source_evaluation": ModelTier.CHEAP,
    # ── STANDARD: everyday analysis ────────────────────────────
    "risk_assessment": ModelTier.STANDARD,
    "trade_execution_plan": ModelTier.STANDARD,
    "position_sizing": ModelTier.STANDARD,
    "portfolio_rebalance": ModelTier.STANDARD,
    "price_alert_analysis": ModelTier.STANDARD,
    # ── STRONG: forecasting, trading decisions ─────────────────
    "market_assessment": ModelTier.STRONG,
    "superforecast": ModelTier.STRONG,
    "best_trade": ModelTier.STRONG,
    "trader_profile": ModelTier.STRONG,
    "copy_trade_eval": ModelTier.STRONG,
    "inverse_position_eval": ModelTier.STRONG,
    "discover_best_trade": ModelTier.STRONG,
    # ── WRITER: long-form ──────────────────────────────────────
    "news_article": ModelTier.WRITER,
}

#: Per-tier defaults: model, temperature, max_tokens, reasoning_effort.
#: ``reasoning_effort`` is only honoured by providers that support it
#: (currently OpenAI).
TIER_DEFAULTS: dict[ModelTier, dict[str, Any]] = {
    ModelTier.CHEAP: {
        "model": "gpt-4o-mini",
        "temperature": 0.0,
        "max_tokens": 512,
        "reasoning_effort": None,
    },
    ModelTier.STANDARD: {
        "model": "gpt-4o-mini",
        "temperature": 0.2,
        "max_tokens": 2048,
        "reasoning_effort": None,
    },
    ModelTier.STRONG: {
        "model": "gpt-4o",
        "temperature": 0.2,
        "max_tokens": 2048,
        "reasoning_effort": "medium",
    },
    ModelTier.WRITER: {
        "model": "gpt-4o",
        "temperature": 0.6,
        "max_tokens": 4096,
        "reasoning_effort": None,
    },
}


class SharedLLMSettings(BaseSettings):
    """LLM settings shared by every deployable.

    Services inherit from this class and add their own fields::

        class Settings(SharedLLMSettings):
            grpc_port: int = 50051
            ...

    The child ``Config`` (env_file, case_sensitive) overrides the
    parent's, so each service keeps reading the same ``.env``.
    """

    # Provider-agnostic LLM configuration
    llm_provider: LLMProvider = LLMProvider.OPENAI
    # Default model. ``gpt-4o-mini`` replaces the retired
    # ``gpt-4-turbo-preview`` default.
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096

    # Per-task model tiering. When false, every task uses the global
    # llm_model / llm_temperature / llm_max_tokens above (kill switch).
    llm_tiering_enabled: bool = True

    # Per-tier overrides. An empty model means "use the tier default";
    # None temperature/max_tokens mean "use the tier default".
    llm_tier_cheap_model: str = ""
    llm_tier_cheap_temperature: float | None = None
    llm_tier_cheap_max_tokens: int | None = None
    llm_tier_standard_model: str = ""
    llm_tier_standard_temperature: float | None = None
    llm_tier_standard_max_tokens: int | None = None
    llm_tier_strong_model: str = ""
    llm_tier_strong_temperature: float | None = None
    llm_tier_strong_max_tokens: int | None = None
    llm_tier_writer_model: str = ""
    llm_tier_writer_temperature: float | None = None
    llm_tier_writer_max_tokens: int | None = None

    # Reasoning effort for the STRONG tier (OpenAI only).
    llm_strong_reasoning_effort: str = "medium"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


def resolve_tier_config(
    task: str, settings: SharedLLMSettings
) -> tuple[str, float, int, str | None]:
    """Resolve the LLM configuration for a task.

    Args:
        task: Task name (a ``TASK_TIERS`` key, e.g. ``"quick_analysis"``).
            Unknown tasks resolve to the STANDARD tier.
        settings: The service settings instance (provides env overrides
            and the ``llm_tiering_enabled`` kill switch).

    Returns:
        ``(model, temperature, max_tokens, reasoning_effort)``. When
        tiering is disabled the global defaults are returned for every
        task. ``reasoning_effort`` is only set for the STRONG tier.
    """
    tier = TASK_TIERS.get(task, ModelTier.STANDARD)
    defaults = TIER_DEFAULTS[tier]

    if not settings.llm_tiering_enabled:
        return (
            settings.llm_model,
            settings.llm_temperature,
            settings.llm_max_tokens,
            None,
        )

    prefix = f"llm_tier_{tier.value}"
    model = getattr(settings, f"{prefix}_model", "") or defaults["model"]

    temperature = getattr(settings, f"{prefix}_temperature", None)
    if temperature is None:
        temperature = defaults["temperature"]

    max_tokens = getattr(settings, f"{prefix}_max_tokens", None)
    if max_tokens is None:
        max_tokens = defaults["max_tokens"]

    reasoning_effort: str | None = None
    if tier is ModelTier.STRONG:
        effort = settings.llm_strong_reasoning_effort
        reasoning_effort = effort or None

    return model, temperature, max_tokens, reasoning_effort
