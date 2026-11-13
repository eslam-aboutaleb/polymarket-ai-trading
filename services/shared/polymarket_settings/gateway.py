"""Portable gateway settings powered by ``llm-gateway-sdk`` and the LiteLLM adapter boundary."""

import hashlib
import json

from llm_gateway_sdk.integrations.pydantic_settings import (
    GatewayBaseSettings,
    connection_options,
)

__all__ = [
    "GatewaySettings",
    "connection_options",
    "create_chat",
    "create_embeddings",
    "embedding_identity",
]


class GatewaySettings(GatewayBaseSettings):
    """Environment names are translated by ``llm-gateway-sdk``; app code uses neutral names."""


def embedding_identity(settings):
    """Persist model, endpoint and dimensions, never keys, with each vector index."""
    model = settings.embedding_model or settings.rag_embedding_model
    base = connection_options(settings, embedding=True).get("api_base", "")
    return hashlib.sha256(
        json.dumps(
            [
                model,
                base,
                settings.embedding_dimensions,
            ]
        ).encode()
    ).hexdigest()


def create_chat(settings, kwargs, model=None):
    from polymarket_settings.litellm_adapter import ProviderRequestError, SafeChatLiteLLM

    options = connection_options(settings)
    kwargs = dict(kwargs)
    kwargs["model_kwargs"] = {**kwargs.get("model_kwargs", {}), "custom_llm_provider": "openai"}
    try:
        return SafeChatLiteLLM(
            model=model or settings.gateway_model or settings.llm_model,
            **options,
            **kwargs,
        )
    except Exception:
        raise ProviderRequestError("Invalid chat provider configuration") from None


def create_embeddings(settings):
    """Send embeddings to the same authenticated gateway as chat requests."""
    from polymarket_settings.litellm_adapter import ProviderRequestError, SafeLiteLLMEmbeddings

    options = connection_options(settings, embedding=True)
    try:
        return SafeLiteLLMEmbeddings(
            model=settings.embedding_model or settings.rag_embedding_model,
            dimensions=settings.embedding_dimensions,
            model_kwargs={
                "custom_llm_provider": "openai",
                **(
                    {"allowed_openai_params": ["dimensions"]}
                    if settings.embedding_dimensions is not None
                    else {}
                ),
            },
            **options,
        )
    except Exception:
        raise ProviderRequestError("Invalid embedding provider configuration") from None
