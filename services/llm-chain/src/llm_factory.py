"""
LLM Factory - Provider-agnostic LLM initialization.
Supports OpenAI, Anthropic, Google, Groq, and Ollama.
Implements Factory pattern for per-request provider selection.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from polymarket_settings import MODEL_CATALOG, LLMProvider, resolve_tier_config

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
    provider = settings.llm_provider

    common_kwargs = {
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "streaming": streaming,
    }

    if callbacks:
        common_kwargs["callbacks"] = callbacks

    if provider == LLMProvider.OPENAI:
        return _create_openai_llm(settings, common_kwargs)
    if provider == LLMProvider.ANTHROPIC:
        return _create_anthropic_llm(settings, common_kwargs)
    if provider == LLMProvider.GOOGLE:
        return _create_google_llm(settings, common_kwargs)
    if provider == LLMProvider.GROQ:
        return _create_groq_llm(settings, common_kwargs)
    if provider == LLMProvider.OLLAMA:
        return _create_ollama_llm(settings, common_kwargs)
    raise ValueError(f"Unsupported LLM provider: {provider}")


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
        if request_config.temperature is not None and request_config.temperature > 0:
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

    if provider == LLMProvider.OPENAI:
        return _create_openai_llm(settings, common_kwargs, model_override, reasoning_effort)
    if provider == LLMProvider.ANTHROPIC:
        return _create_anthropic_llm(settings, common_kwargs, model_override)
    if provider == LLMProvider.GOOGLE:
        return _create_google_llm(settings, common_kwargs, model_override)
    if provider == LLMProvider.GROQ:
        return _create_groq_llm(settings, common_kwargs, model_override)
    if provider == LLMProvider.OLLAMA:
        return _create_ollama_llm(settings, common_kwargs, model_override)
    raise ValueError(f"Unsupported LLM provider: {provider}")


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
        if llm_override and llm_override.temperature is not None and llm_override.temperature > 0
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
        else tier_reasoning
    )

    common_kwargs = {
        "temperature": temperature,
        "max_tokens": max_tokens,
        "streaming": streaming,
    }
    if callbacks:
        common_kwargs["callbacks"] = callbacks

    if provider == LLMProvider.OPENAI:
        return _create_openai_llm(settings, common_kwargs, model_override, reasoning_effort)
    if provider == LLMProvider.ANTHROPIC:
        return _create_anthropic_llm(settings, common_kwargs, model_override)
    if provider == LLMProvider.GOOGLE:
        return _create_google_llm(settings, common_kwargs, model_override)
    if provider == LLMProvider.GROQ:
        return _create_groq_llm(settings, common_kwargs, model_override)
    if provider == LLMProvider.OLLAMA:
        return _create_ollama_llm(settings, common_kwargs, model_override)
    raise ValueError(f"Unsupported LLM provider: {provider}")


def _create_openai_llm(
    settings,
    kwargs,
    model_override: str | None = None,
    reasoning_effort: str | None = None,
) -> BaseChatModel:
    """Create OpenAI ChatGPT instance."""
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for OpenAI provider")

    from langchain_openai import ChatOpenAI

    model = model_override or settings.llm_model
    if reasoning_effort:
        kwargs = {**kwargs, "model_kwargs": {"reasoning_effort": reasoning_effort}}

    return ChatOpenAI(model=model, api_key=settings.openai_api_key, **kwargs)


def _create_anthropic_llm(settings, kwargs, model_override: str | None = None) -> BaseChatModel:
    """Create Anthropic Claude instance."""
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required for Anthropic provider")

    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        raise ImportError(
            "langchain-anthropic not installed. Install with: pip install langchain-anthropic"
        ) from None

    model = model_override or settings.llm_model

    return ChatAnthropic(model=model, api_key=settings.anthropic_api_key, **kwargs)


def _create_google_llm(settings, kwargs, model_override: str | None = None) -> BaseChatModel:
    """Create Google Gemini instance."""
    if not settings.google_api_key:
        raise ValueError("GOOGLE_API_KEY is required for Google provider")

    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        raise ImportError(
            "langchain-google-genai not installed. Install with: pip install langchain-google-genai"
        ) from None

    model = model_override or settings.llm_model

    return ChatGoogleGenerativeAI(model=model, google_api_key=settings.google_api_key, **kwargs)


def _create_groq_llm(settings, kwargs, model_override: str | None = None) -> BaseChatModel:
    """Create Groq instance (fast inference)."""
    if not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY is required for Groq provider")

    try:
        from langchain_groq import ChatGroq
    except ImportError:
        raise ImportError(
            "langchain-groq not installed. Install with: pip install langchain-groq"
        ) from None

    model = model_override or settings.llm_model

    return ChatGroq(model=model, api_key=settings.groq_api_key, **kwargs)


def _create_ollama_llm(settings, kwargs, model_override: str | None = None) -> BaseChatModel:
    """Create Ollama instance (local models)."""
    try:
        from langchain_community.chat_models import ChatOllama
    except ImportError:
        raise ImportError(
            "langchain-community not installed. Install with: pip install langchain-community"
        ) from None

    # Ollama doesn't use max_tokens the same way
    kwargs.pop("max_tokens", None)

    model = model_override or settings.llm_model

    return ChatOllama(model=model, base_url=settings.ollama_base_url, **kwargs)


def get_provider_info() -> dict:
    """Get information about the configured LLM provider."""
    settings = get_settings()
    return {
        "provider": settings.llm_provider.value,
        "model": settings.llm_model,
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


def get_available_providers() -> dict[str, dict[str, Any]]:
    """
    Get all available LLM providers and their configuration status.

    Returns:
        Dictionary of provider info with availability status
    """
    settings = get_settings()

    return {
        LLMProvider.OPENAI.value: {
            "name": "OpenAI",
            "configured": bool(settings.openai_api_key),
            "models": MODEL_CATALOG.get(LLMProvider.OPENAI, []),
            "description": "OpenAI GPT models (GPT-4, GPT-4o, etc.)",
        },
        LLMProvider.ANTHROPIC.value: {
            "name": "Anthropic",
            "configured": bool(settings.anthropic_api_key),
            "models": MODEL_CATALOG.get(LLMProvider.ANTHROPIC, []),
            "description": "Anthropic Claude models",
        },
        LLMProvider.GOOGLE.value: {
            "name": "Google",
            "configured": bool(settings.google_api_key),
            "models": MODEL_CATALOG.get(LLMProvider.GOOGLE, []),
            "description": "Google Gemini models",
        },
        LLMProvider.GROQ.value: {
            "name": "Groq",
            "configured": bool(settings.groq_api_key),
            "models": MODEL_CATALOG.get(LLMProvider.GROQ, []),
            "description": "Groq fast inference (Llama, Mixtral)",
        },
        LLMProvider.OLLAMA.value: {
            "name": "Ollama",
            "configured": True,  # Ollama doesn't need API key, just needs to be running
            "base_url": settings.ollama_base_url,
            "models": MODEL_CATALOG.get(LLMProvider.OLLAMA, []),
            "description": "Local Ollama models",
        },
    }
