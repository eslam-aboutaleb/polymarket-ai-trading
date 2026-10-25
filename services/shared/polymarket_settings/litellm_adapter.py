"""LangChain clients that keep dependency credentials and failures private."""

import litellm
from langchain_litellm import ChatLiteLLM, LiteLLMEmbeddings
from pydantic import Field

# Provider exceptions can contain response bodies and headers. Never let them
# reach service logs or API error responses. Leave TLS verification enabled.
litellm.suppress_debug_info = True
litellm.turn_off_message_logging = True


class ProviderRequestError(RuntimeError):
    """Safe error crossing the backend model boundary."""


class SafeChatLiteLLM(ChatLiteLLM):
    api_key: str | None = Field(default=None, repr=False, exclude=True)

    def completion_with_retry(self, *args, **kwargs):
        try:
            return super().completion_with_retry(*args, **kwargs)
        except Exception:
            raise ProviderRequestError("Chat provider request failed") from None

    async def acompletion_with_retry(self, *args, **kwargs):
        try:
            return await super().acompletion_with_retry(*args, **kwargs)
        except Exception:
            raise ProviderRequestError("Chat provider request failed") from None

    def _stream(self, *args, **kwargs):
        try:
            yield from super()._stream(*args, **kwargs)
        except Exception:
            raise ProviderRequestError("Chat provider stream failed") from None

    async def _astream(self, *args, **kwargs):
        try:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
        except Exception:
            raise ProviderRequestError("Chat provider stream failed") from None


class SafeLiteLLMEmbeddings(LiteLLMEmbeddings):
    api_key: str | None = Field(default=None, repr=False, exclude=True)

    def _embedding_with_retry(self, **kwargs):
        try:
            return super()._embedding_with_retry(**kwargs)
        except Exception:
            raise ProviderRequestError("Embedding provider request failed") from None

    async def _aembedding_with_retry(self, **kwargs):
        try:
            return await super()._aembedding_with_retry(**kwargs)
        except Exception:
            raise ProviderRequestError("Embedding provider request failed") from None
