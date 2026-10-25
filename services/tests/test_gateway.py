"""Keyless configuration, dependency boundary, and opt-in synthetic live checks."""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "llm-chain"))

from polymarket_settings.gateway import connection_options, create_embeddings  # noqa: E402
from src.config import Settings  # noqa: E402


@pytest.fixture
def settings(monkeypatch):
    for name in os.environ:
        if name.startswith(("LITELLM_", "LLM_", "RAG_EMBEDDING_")):
            monkeypatch.delenv(name)
    return Settings(_env_file=None)


def test_connection_inheritance_and_explicit_empty_endpoint(settings):
    assert connection_options(settings) == {}
    assert connection_options(settings, embedding=True) == {}
    settings = Settings(
        _env_file=None, gateway_base="https://gateway.example/v1", gateway_key="synthetic-shared"
    )
    assert connection_options(settings, embedding=True) == connection_options(settings)
    overridden = settings.model_copy(
        update={
            "embedding_base": settings.gateway_base.__class__(""),
            "embedding_key": settings.gateway_key.__class__("synthetic-independent"),
        }
    )
    assert connection_options(overridden, embedding=True) == {"api_key": "synthetic-independent"}
    assert connection_options(settings)["api_base"] == "https://gateway.example/v1"


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://localhost/v1",
        "https://user:synthetic-secret@localhost/v1",
        "https://localhost/v1?api_key=synthetic-secret",
        "https://localhost/v1#synthetic-secret",
        "https://localhost/v1?",
        "https://localhost/v1#",
        "https://localhost:99999/v1",
        "https://localhost/\nsynthetic-secret",
        "https://localhost\\@evil.example/v1",
    ],
)
@pytest.mark.parametrize("field", ["gateway_base", "embedding_base"])
def test_invalid_endpoints_and_errors_are_private(settings, endpoint, field):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, gateway_key="synthetic-secret", **{field: endpoint})
    assert "synthetic-secret" not in str(caught.value)
    assert "synthetic-secret" not in caught.value.json()
    assert "synthetic-secret" not in str(caught.value.errors())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:4000/v1",
        "http://127.0.0.1/v1",
        "http://[::1]:4000/v1",
        "https://10.0.0.5:8443/path/v1",
        "http://provider.internal/v1",
    ],
)
def test_private_endpoints_are_supported(settings, endpoint):
    configured = Settings(_env_file=None, gateway_base=endpoint)
    assert connection_options(configured)["api_base"] == endpoint


def test_secrets_masked_and_precedence(settings, monkeypatch):
    monkeypatch.setenv("LITELLM_MODEL", "openai/custom-chat")
    monkeypatch.setenv("LLM_MODEL", "legacy-chat")
    monkeypatch.setenv("LITELLM_EMBEDDING_MODEL", "openai/custom-embedding")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "legacy-embedding")
    monkeypatch.setenv("LITELLM_EMBEDDING_DIMENSIONS", "256")
    monkeypatch.setenv("RAG_EMBEDDING_DIMENSIONS", "512")
    configured = Settings(_env_file=None, gateway_key="synthetic-secret")
    assert configured.gateway_model == "openai/custom-chat"
    assert configured.embedding_model == "openai/custom-embedding"
    assert configured.embedding_dimensions == 256
    assert "synthetic-secret" not in repr(configured)
    assert "synthetic-secret" not in configured.model_dump_json()
    assert "synthetic-secret" not in str(configured.model_dump())
    monkeypatch.delenv("LITELLM_EMBEDDING_DIMENSIONS")
    assert Settings(_env_file=None).embedding_dimensions == 512
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, gateway_key="synthetic-secret", embedding_dimensions=0)
    assert "synthetic-secret" not in caught.value.json()


def test_real_factories_and_tools_without_network(settings, monkeypatch):
    pytest.importorskip("langchain_litellm")
    from src import llm_factory

    configured = Settings(
        _env_file=None,
        llm_provider="litellm",
        gateway_model="openai/custom-chat",
        gateway_key="synthetic-secret",
        gateway_base="https://gateway.example/v1",
        embedding_model="openai/custom-embedding",
        embedding_dimensions=256,
    )
    monkeypatch.setattr(llm_factory, "get_settings", lambda: configured)
    for factory in [
        llm_factory.create_llm,
        llm_factory.create_llm_for_request,
        lambda: llm_factory.create_llm_for_task("market_assessment"),
    ]:
        client = factory()
        assert client.model == "openai/custom-chat"
        assert client.api_base == "https://gateway.example/v1"
        assert "synthetic-secret" not in repr(client)
        assert "synthetic-secret" not in str(client.model_dump())
        assert "synthetic-secret" not in str(client.to_json())
    configured.llm_tier_strong_model = "openai/strong-custom"
    assert llm_factory.create_llm_for_task("market_assessment").model == "openai/strong-custom"
    request = llm_factory.LLMRequestConfig(model="openai/request-custom")
    assert (
        llm_factory.create_llm_for_task("market_assessment", llm_override=request).model
        == request.model
    )
    client = llm_factory.create_llm(streaming=True)
    assert client.streaming
    tool = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo text",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    }
    assert client.bind_tools([tool]).kwargs["tools"][0]["function"]["name"] == "echo"
    embeddings = create_embeddings(configured)
    assert embeddings.model == "openai/custom-embedding"
    assert embeddings.dimensions == 256
    assert embeddings._get_litellm_params()["allowed_openai_params"] == ["dimensions"]
    assert "synthetic-secret" not in str(embeddings.model_dump())


def test_native_resolution_and_independent_embeddings(settings):
    pytest.importorskip("langchain_litellm")
    from polymarket_settings.gateway import create_chat

    configured = Settings(_env_file=None, gateway_model="anthropic/native-model")
    client = create_chat(configured, {})
    assert client.api_base is None
    assert client.api_key is None
    configured.embedding_model = "openai/custom-embedding"
    embeddings = create_embeddings(configured)
    assert embeddings.api_key is None
    assert embeddings.api_base is None


def test_provider_failures_do_not_reach_logs(settings, monkeypatch, caplog):
    pytest.importorskip("langchain_litellm")
    import litellm
    from polymarket_settings.gateway import create_chat
    from polymarket_settings.litellm_adapter import ProviderRequestError

    def fail(**kwargs):
        raise RuntimeError("synthetic-secret in provider response")

    monkeypatch.setattr(litellm, "completion", fail)
    monkeypatch.setattr(litellm, "embedding", fail)
    client = create_chat(settings, {"max_retries": 1})
    with pytest.raises(ProviderRequestError) as caught:
        client.invoke("synthetic input")
    assert "synthetic-secret" not in str(caught.value)
    settings.embedding_model = "openai/custom-embedding"
    with pytest.raises(ProviderRequestError) as caught:
        create_embeddings(settings).embed_query("synthetic input")
    assert "synthetic-secret" not in str(caught.value)
    assert "synthetic-secret" not in caplog.text


def test_rag_rejects_changed_models_dimensions_and_unversioned_indexes(
    settings, tmp_path, monkeypatch
):
    from src import market_rag

    class FakeEmbeddings:
        def __init__(self, **_kwargs):
            pass

        def embed_documents(self, texts):
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, _text):
            return [1.0, 0.0]

    settings.chroma_persist_dir = str(tmp_path)
    monkeypatch.setattr(market_rag, "get_settings", lambda: settings)
    monkeypatch.setattr(market_rag, "_OpenAIEmbeddings", FakeEmbeddings)
    store = market_rag.MarketRAGService()
    market = {"condition_id": "synthetic", "question": "Synthetic rates question"}
    assert store.index_markets([market], force=True) == 1
    assert store.search("rates")
    settings.rag_embedding_model = "different-model-same-dimensions"
    assert store.index_markets([market], force=True) == 0
    assert store.search("rates") == []
    assert store.get_collection_size() == 1
    store.clear()
    settings.embedding_dimensions = 3
    assert store.index_markets([market], force=True) == 0
    settings.embedding_dimensions = 2
    assert store.index_markets([market], force=True) == 1
    with sqlite3.connect(store._database_path) as db:
        db.execute("DELETE FROM vector_config")
    assert store.search("rates") == []
    assert store.index_markets([market], force=True) == 0


@pytest.mark.skipif(
    os.getenv("RUN_GATEWAY_INTEGRATION") != "1", reason="explicit live opt-in required"
)
def test_live_gateway_factories_and_rag(tmp_path, monkeypatch):
    """Consumes only synthetic inputs; no model output, token, or vectors are logged."""
    from src import llm_factory, market_rag

    configured = Settings(_env_file=None, chroma_persist_dir=str(tmp_path), rag_enabled=True)
    monkeypatch.setattr(llm_factory, "get_settings", lambda: configured)
    monkeypatch.setattr(market_rag, "get_settings", lambda: configured)
    client = llm_factory.create_llm_for_task("quick_analysis")
    assert client.invoke("Reply with OK.").content
    assert any(chunk.content for chunk in llm_factory.create_llm(streaming=True).stream("Say OK."))
    tool = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo text",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }
    tool_client = llm_factory.create_llm_for_request(
        llm_factory.LLMRequestConfig(
            model=os.getenv("GATEWAY_TEST_TOOL_MODEL", configured.gateway_model), max_tokens=128
        )
    )
    reply = tool_client.bind_tools([tool], tool_choice="required").invoke("Call echo with text OK.")
    assert reply.tool_calls and reply.tool_calls[0]["name"] == "echo"
    assert json.loads(reply.additional_kwargs["tool_calls"][0]["function"]["arguments"])["text"]
    store = market_rag.MarketRAGService()
    assert (
        store.index_markets(
            [{"condition_id": "synthetic", "question": "Synthetic rates question"}], force=True
        )
        == 1
    )
    assert store.search("Synthetic rates question")[0]["condition_id"] == "synthetic"

    async def verify_async():
        assert (await client.ainvoke("Reply with OK.")).content
        chunks = [
            chunk async for chunk in llm_factory.create_llm(streaming=True).astream("Say OK.")
        ]
        assert any(chunk.content for chunk in chunks)
        assert (
            len(await create_embeddings(configured).aembed_query("Synthetic check."))
            == configured.embedding_dimensions
        )

    asyncio.run(verify_async())


def test_invalid_dependency_configuration_does_not_leak_key(settings):
    pytest.importorskip("langchain_litellm")
    from polymarket_settings.gateway import create_chat
    from polymarket_settings.litellm_adapter import ProviderRequestError

    settings.gateway_key = SecretStr("synthetic-secret")
    with pytest.raises(ProviderRequestError) as caught:
        create_chat(settings, {"temperature": -1})
    assert "synthetic-secret" not in str(caught.value)


def test_environment_embedding_override_distinguishes_unset_from_empty(settings, monkeypatch):
    monkeypatch.setenv("LITELLM_API_BASE", "http://localhost:4000/v1")
    monkeypatch.setenv("LITELLM_API_KEY", "synthetic-shared")
    monkeypatch.setenv("LITELLM_EMBEDDING_API_BASE", "")
    configured = Settings(_env_file=None)
    assert connection_options(configured, embedding=True) == {"api_key": "synthetic-shared"}
    monkeypatch.delenv("LITELLM_EMBEDDING_API_BASE")
    configured = Settings(_env_file=None)
    assert connection_options(configured, embedding=True)["api_base"] == "http://localhost:4000/v1"
    monkeypatch.setenv("LITELLM_EMBEDDING_API_BASE", "http://127.0.0.1:5000/v1")
    monkeypatch.setenv("LITELLM_EMBEDDING_API_KEY", "synthetic-independent")
    configured = Settings(_env_file=None)
    assert connection_options(configured, embedding=True) == {
        "api_base": "http://127.0.0.1:5000/v1",
        "api_key": "synthetic-independent",
    }


def test_tool_call_reaches_sdk_transport_without_network(settings):
    pytest.importorskip("langchain_litellm")
    import httpx
    from openai import OpenAI
    from polymarket_settings.gateway import create_chat

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "custom-chat",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "synthetic-call",
                                    "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text":"OK"}'},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    configured = Settings(_env_file=None, gateway_model="openai/custom-chat")
    tool = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo text",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    }
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        sdk = OpenAI(api_key="synthetic-test-key", http_client=transport)
        reply = create_chat(configured, {}).bind_tools([tool]).invoke("Synthetic input", client=sdk)
    assert requests[0]["model"] == "custom-chat"
    assert requests[0]["tools"][0]["function"]["name"] == "echo"
    assert reply.tool_calls[0]["args"] == {"text": "OK"}
