"""Centralized LLM settings shared by every Polymarket deployable.

``llm-chain``, ``cli-agent`` and the backend all need the same notion of
LLM providers, model tiers and per-task model routing. Previously each
service carried its own copy of these definitions and the copies had
drifted apart (stale ``gpt-4-turbo-preview`` defaults, three different
``LLMProvider`` enums, no per-task tiering).

This package is the single canonical implementation, following the
``polymarket_mcp`` shared-package precedent: both AI-service Dockerfiles
already copy ``services/shared/`` onto ``PYTHONPATH``, and the backend
mounts it via a read-only volume. Services import it as::

    from polymarket_settings import LLMProvider, SharedLLMSettings

Each service's own ``config.py`` declares ``class Settings(SharedLLMSettings)``
and keeps only its service-specific fields, so shared behaviour (defaults,
env var names, tier resolution) cannot diverge between services.
"""

from polymarket_settings.settings import (
    MODEL_CATALOG,
    TASK_TIERS,
    TIER_DEFAULTS,
    LLMProvider,
    ModelTier,
    SharedLLMSettings,
    resolve_tier_config,
)

__all__ = [
    "LLMProvider",
    "MODEL_CATALOG",
    "ModelTier",
    "TASK_TIERS",
    "TIER_DEFAULTS",
    "SharedLLMSettings",
    "resolve_tier_config",
]
