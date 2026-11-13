"""
LLM initialization through the authenticated LiteLLM gateway.
Provider preferences select model aliases; every transport uses the gateway.
"""

from dataclasses import dataclass

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from polymarket_settings import MODEL_CATALOG, LLMProvider, resolve_tier_config
from polymarket_settings.gateway import create_chat

from src.config import get_settings


@dataclass
class LLMRequestConfig:
    """Per-request LLM configuration (from gRPC LLMConfig message)."""

    provider: LLMProvider | None = None  # None = use service default
    model: str | None = None  # None = use provider default
    temperature: float | None = None  # None = use service default
    max_tokens: int | None = None  # None = use service default
    reasoning_effort: str | None = None  # OpenAI reasoning models only


def create_llm(
    streaming: bool = False, callbacks: list[BaseCallbackHandler] | None = None
) -> BaseChatModel:
    """
    Factory function to create an LLM based on configuration.

    Args:
        streaming: Enable streaming responses
        callbacks: Optional callback handlers

    Returns:
        Configured LLM instance

    Raises:
        ValueError: If provider is not supported or API key is missing
    """
    settings = get_settings()

    common_kwargs = {
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "streaming": streaming,
    }

    if callbacks:
        common_kwargs["callbacks"] = callbacks

    return create_chat(settings, common_kwargs)


def create_llm_for_request(
    request_config: LLMRequestConfig | None = None,
    streaming: bool = False,
    callbacks: list[BaseCallbackHandler] | None = None,
) -> BaseChatModel:
    """
    Factory function to create an LLM with per-request configuration override.

    This enables the gateway pattern where each request can specify its own
    provider and model, falling back to service defaults when not specified.

    Args:
        request_config: Per-request LLM configuration (provider, model, etc.)
        streaming: Enable streaming responses
        callbacks: Optional callback handlers

    Returns:
        Configured LLM instance

    Raises:
        ValueError: If provider is not supported or API key is missing
    """
    settings = get_settings()

    # Determine provider: request override → service default
    if request_config and request_config.provider:
        provider = request_config.provider
    else:
        provider = settings.llm_provider

    # Determine temperature and max_tokens with overrides
    temperature = settings.llm_temperature
    max_tokens = settings.llm_max_tokens

    if request_config:
        if request_config.temperature is not None and request_config.temperature >= 0:
            temperature = request_config.temperature
        if request_config.max_tokens is not None and request_config.max_tokens > 0:
            max_tokens = request_config.max_tokens

    common_kwargs = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "streaming": streaming,
    }

    if callbacks:
        common_kwargs["callbacks"] = callbacks

    # Determine model: request override → provider default
    model_override = None
    if request_config and request_config.model:
        model_override = request_config.model
    elif provider == LLMProvider.LITELLM:
        model_override = settings.gateway_model or settings.llm_model
    elif (
        request_config
        and request_config.provider
        and request_config.provider != settings.llm_provider
    ):
        # The tier/global defaults are OpenAI models. A cross-provider
        # request must not force an OpenAI model name onto Anthropic,
        # Google, Groq or Ollama.
        provider_models = MODEL_CATALOG.get(request_config.provider, [])
        model_override = provider_models[0] if provider_models else None

    reasoning_effort = None
    if request_config and request_config.reasoning_effort:
        reasoning_effort = request_config.reasoning_effort

    if reasoning_effort:
        common_kwargs["model_kwargs"] = {"reasoning_effort": reasoning_effort}
    return create_chat(settings, common_kwargs, model_override)


def create_llm_for_task(
    task_name: str,
    streaming: bool = False,
    callbacks: list[BaseCallbackHandler] | None = None,
    llm_override: LLMRequestConfig | None = None,
) -> BaseChatModel:
    """Create the LLM configured for a task's model tier.

    A per-call ``llm_override`` wins over the tier defaults. The
    tier defaults come from ``resolve_tier_config`` and can be
    disabled with ``LLM_TIERING_ENABLED=false``.
    """
    settings = get_settings()
    tier_model, tier_temperature, tier_max_tokens, tier_reasoning = resolve_tier_config(
        task_name, settings
    )

    provider = (
        llm_override.provider if llm_override and llm_override.provider else settings.llm_provider
    )
    if llm_override and llm_override.model:
        model_override = llm_override.model
    elif provider == LLMProvider.LITELLM:
        from polymarket_settings import TASK_TIERS, ModelTier

        tier = TASK_TIERS.get(task_name, ModelTier.STANDARD)
        configured_tier = (
            getattr(settings, f"llm_tier_{tier.value}_model", "")
            if settings.llm_tiering_enabled
            else ""
        )
        model_override = configured_tier or settings.gateway_model or settings.llm_model
    elif provider == LLMProvider.OPENAI:
        # Tier defaults are OpenAI models.
        model_override = tier_model
    else:
        # A non-OpenAI provider must not receive an OpenAI model
        # name from the OpenAI-centric tier defaults.
        provider_models = MODEL_CATALOG.get(provider, [])
        model_override = provider_models[0] if provider_models else None
    temperature = (
        llm_override.temperature
        if llm_override and llm_override.temperature is not None and llm_override.temperature >= 0
        else tier_temperature
    )
    max_tokens = (
        llm_override.max_tokens
        if llm_override and llm_override.max_tokens is not None and llm_override.max_tokens > 0
        else tier_max_tokens
    )
    reasoning_effort = (
        llm_override.reasoning_effort
        if llm_override and llm_override.reasoning_effort
        else (None if provider == LLMProvider.LITELLM else tier_reasoning)
    )

    common_kwargs = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "streaming": streaming,
    }
    if callbacks:
        common_kwargs["callbacks"] = callbacks

    if reasoning_effort:
        common_kwargs["model_kwargs"] = {"reasoning_effort": reasoning_effort}
    return create_chat(settings, common_kwargs, model_override)


def get_provider_info() -> dict:
    """Get information about the configured LLM provider."""
    settings = get_settings()
    return {
        "provider": LLMProvider.LITELLM.value,
        "model": settings.gateway_model or settings.llm_model,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }


def parse_llm_config_from_proto(llm_config) -> LLMRequestConfig | None:
    """
    Parse a gRPC LLMConfig message into LLMRequestConfig.

    Args:
        llm_config: Proto LLMConfig message (can be None or empty)

    Returns:
        LLMRequestConfig or None if no overrides specified
    """
    if not llm_config:
        return None

    # Map proto enum to LLMProvider
    # Proto enum: LLM_PROVIDER_DEFAULT=0, OPENAI=1, ANTHROPIC=2, GOOGLE=3, GROQ=4, OLLAMA=5
    PROTO_TO_PROVIDER = {
        0: None,  # DEFAULT - use service config
        1: LLMProvider.OPENAI,
        2: LLMProvider.ANTHROPIC,
        3: LLMProvider.GOOGLE,
        4: LLMProvider.GROQ,
        5: LLMProvider.OLLAMA,
        7: LLMProvider.LITELLM,
        # 6 = GITHUB is handled by CLI-agent, not llm-chain
    }

    provider = PROTO_TO_PROVIDER.get(llm_config.provider)
    model = llm_config.model if llm_config.model else None
    temperature = llm_config.temperature if llm_config.temperature > 0 else None
    max_tokens = llm_config.max_tokens if llm_config.max_tokens > 0 else None

    # If all are None/default, return None
    if provider is None and model is None and temperature is None and max_tokens is None:
        return None

    return LLMRequestConfig(
        provider=provider, model=model, temperature=temperature, max_tokens=max_tokens
    )


def get_available_providers() -> dict[str, dict]:
    """Advertise the authenticated gateway; upstream providers live behind it."""
    settings = get_settings()
    return {
        LLMProvider.LITELLM.value: {
            "name": "LiteLLM",
            "configured": bool(
                settings.gateway_base
                and settings.gateway_key
                and settings.gateway_base.get_secret_value()
                and settings.gateway_key.get_secret_value()
            ),
            "models": [settings.gateway_model or settings.llm_model],
            "description": "Authenticated LiteLLM gateway",
        }
    }
