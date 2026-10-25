"""Portable gateway settings and the sole LiteLLM dependency boundary."""

from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """Environment names are translated here; application code uses neutral names."""

    gateway_base: SecretStr | None = Field(default=None, validation_alias="LITELLM_API_BASE")
    gateway_key: SecretStr | None = Field(default=None, validation_alias="LITELLM_API_KEY")
    gateway_model: str | None = Field(default=None, validation_alias="LITELLM_MODEL")
    embedding_model: str | None = Field(default=None, validation_alias="LITELLM_EMBEDDING_MODEL")
    embedding_key: SecretStr | None = Field(
        default=None, validation_alias="LITELLM_EMBEDDING_API_KEY"
    )
    embedding_base: SecretStr | None = Field(
        default=None, validation_alias="LITELLM_EMBEDDING_API_BASE"
    )
    embedding_dimensions: int | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("LITELLM_EMBEDDING_DIMENSIONS", "RAG_EMBEDDING_DIMENSIONS"),
    )

    model_config = SettingsConfigDict(populate_by_name=True, hide_input_in_errors=True)

    @model_validator(mode="wrap")
    @classmethod
    def redact_validation_errors(cls, values, handler):
        """Pydantic's errors()/json() must not retain credential-bearing inputs."""
        try:
            return handler(values)
        except ValidationError as exc:
            raise ValidationError.from_exception_data(
                cls.__name__,
                [
                    {
                        "type": "value_error",
                        "loc": error["loc"],
                        "input": "[redacted]",
                        "ctx": {"error": ValueError("Invalid configuration value")},
                    }
                    for error in exc.errors()
                ],
                hide_input=True,
            ) from None

    @field_validator("gateway_base", "embedding_base")
    @classmethod
    def validate_endpoint(cls, value):
        if value is None or value.get_secret_value() == "":
            return value
        raw = value.get_secret_value()
        try:
            url = urlsplit(raw)
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and url.port != 0
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and "?" not in raw
                and "#" not in raw
                and not any(char.isspace() or ord(char) < 32 for char in raw)
                and "\\" not in raw
                and "@" not in url.netloc
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Endpoint must be an HTTP(S) URL without credentials, query or fragment"
            )
        return value


def _value(secret):
    return secret.get_secret_value() if secret is not None else None


def connection_options(settings, *, embedding=False):
    """Omit absent options so LiteLLM retains provider-native resolution.

    None embedding overrides inherit; an empty endpoint explicitly cancels
    inheritance. Empty keys defer to native authentication rather than sentinels.
    """
    base = settings.gateway_base
    key = settings.gateway_key
    if embedding:
        base = settings.embedding_base if settings.embedding_base is not None else base
        key = settings.embedding_key if settings.embedding_key is not None else key
    return {
        name: value
        for name, value in (("api_base", _value(base)), ("api_key", _value(key)))
        if value
    }


def embedding_identity(settings):
    """Persist model, endpoint and dimensions, never keys, with each vector index."""
    import hashlib
    import json

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

    try:
        return SafeChatLiteLLM(
            model=model or settings.gateway_model or settings.llm_model,
            **connection_options(settings),
            **kwargs,
        )
    except Exception:
        raise ProviderRequestError("Invalid chat provider configuration") from None


def create_embeddings(settings, legacy_factory=None):
    """The independent embedding alias opts into LiteLLM even with legacy chat."""
    if settings.llm_provider.value == "litellm" or settings.embedding_model:
        from polymarket_settings.litellm_adapter import ProviderRequestError, SafeLiteLLMEmbeddings

        try:
            return SafeLiteLLMEmbeddings(
                model=settings.embedding_model or settings.rag_embedding_model,
                dimensions=settings.embedding_dimensions,
                # Custom aliases are absent from LiteLLM's model capability
                # catalog. An explicit dimension setting declares support;
                # forward it instead of silently dropping it or rejecting it.
                model_kwargs={"allowed_openai_params": ["dimensions"]}
                if settings.embedding_dimensions is not None
                else {},
                **connection_options(settings, embedding=True),
            )
        except Exception:
            raise ProviderRequestError("Invalid embedding provider configuration") from None
    if legacy_factory is None:
        from langchain_openai import OpenAIEmbeddings

        legacy_factory = OpenAIEmbeddings
    kwargs = {"model": settings.rag_embedding_model, "openai_api_key": settings.openai_api_key}
    if settings.embedding_dimensions is not None:
        kwargs["dimensions"] = settings.embedding_dimensions
    return legacy_factory(**kwargs)
