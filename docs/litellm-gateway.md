# LiteLLM gateway configuration

All chat and embedding requests from `llm-chain` and `cli-agent` use the same
LiteLLM gateway. Provider preferences still select models, but cannot select a
direct provider connection. Missing gateway credentials cause an error; native
OpenAI, GitHub, Anthropic, Google, Groq and Ollama credentials are never a fallback.

Configure the root `.env` for Docker Compose, or the service environment:

```dotenv
LLM_PROVIDER=litellm
LITELLM_API_BASE=https://gateway.example/v1
LITELLM_API_KEY=<virtual-key>
LITELLM_MODEL=<chat-alias>
LITELLM_EMBEDDING_MODEL=<embedding-alias>
# Optional, when supported by the embedding model:
# LITELLM_EMBEDDING_DIMENSIONS=1536
```

Both credentials are required. Model aliases must exist on the gateway; plain
aliases work because the adapter explicitly selects the OpenAI-compatible
protocol. `LITELLM_MODEL` falls back to `LLM_MODEL`, and the embedding model falls
back to `RAG_EMBEDDING_MODEL`. Legacy embedding endpoint/key overrides are ignored:
embeddings always use `LITELLM_API_BASE` and `LITELLM_API_KEY`.

Explicit request models take precedence over configured task-tier models, then
`LITELLM_MODEL`, then `LLM_MODEL`. Tier aliases require `LLM_TIERING_ENABLED=true`.
Provider-specific requests can select the existing provider catalog's models;
those model names must also be available on the gateway. Sampling, reasoning and
token limits follow the task/request configuration. Streaming, callbacks,
structured output and tools use the same authenticated chat adapter.

Docker Compose interpolates the root `.env` into both AI services. For a service
started outside Docker, export these variables or populate that service's `.env`.
Keep credentials out of frontend variables, request payloads and tracked files.
The root and local LLM-service `.env` files are ignored by Git.

The repository has GitHub Actions secrets named `LITELLM_API_BASE` and
`LITELLM_API_KEY`. GitHub only returns secret metadata, never stored values.
Populate local `.env` values from the original secret source. The manual real
user experience workflow references these secrets directly inside GitHub Actions.

Endpoint validation permits HTTP(S) and rejects embedded credentials, query
strings and fragments. TLS verification stays enabled. Credential representations
and provider failure messages are redacted.

## Vector compatibility and migration

The existing index is SQLite, with JSON vector values rather than a fixed-width
SQL vector column. No vector column alteration is required. New writes persist a
fingerprint of model alias, explicit endpoint and requested dimensions alongside
the observed vector length. Both ingestion and retrieval reject a nonempty index
with a different fingerprint/dimension or missing metadata. Ingestion checks the
whole batch before upserting and commits metadata and vectors together.
A write transaction locks compatibility checking and upserting together, so
concurrent workers cannot initialize different embedding spaces in one
collection. Retrieval reads metadata and vectors from one database snapshot.

Existing nonempty indexes predate this metadata and require a **full reindex**.
Stop indexing workers, back up the SQLite file, select a new
`CHROMA_COLLECTION_NAME` (or clear the old collection through
`MarketRAGService.clear()`), and run the existing `IndexMarketsRAG` ingestion path
with the complete market corpus. Reindex after changing model, dimensions or
endpoint, even if vector lengths happen to match. Do not partially overwrite an
old collection. The same alias silently changing its upstream embedding model
cannot be detected; gateway operators must coordinate a fresh collection.

RAG `search()` and `multi_query_search()` use the same embedding adapter as
`index_markets()`. As before, the production gRPC API currently exposes indexing
but has no consumer of those search helpers. This integration verifies the
retrieval boundary directly; it does not introduce a new product retrieval flow.
Disabled RAG still avoids embedding and query-expansion calls.

## Verification and CI

Ordinary CI is keyless. It runs configuration/security/schema tests with the
backend dependencies and adapter tests against the locked LLM-service dependencies.
No pull-request job references production gateway secrets. The manual real user experience workflow uses the repository gateway secrets;
ordinary pull-request jobs use synthetic credentials and mocked transports.

An explicit local live check uses synthetic text, a temporary SQLite directory,
chat, streaming, tool calls, embeddings and ingestion/retrieval:

```sh
PYTHONPATH=services/shared:services/llm-chain RUN_GATEWAY_INTEGRATION=1 \
  uv run --project services/llm-chain --frozen pytest \
  services/tests/test_gateway.py::test_live_gateway_factories_and_rag -q
```

Supply gateway variables securely in the process environment first.
`GATEWAY_TEST_TOOL_MODEL` optionally selects a tool-capable alias independently of
chat. No live check runs unless explicitly opted in. Do not attach production
secrets to `pull_request_target`, fork checkouts or an unrestricted manual workflow.
Live checks consume the virtual key's budget and rate limit.

Dependency references: [LiteLLM SDK](https://docs.litellm.ai/docs/) and
[LangChain ChatLiteLLM](https://reference.langchain.com/python/langchain-litellm/chat_models/litellm/ChatLiteLLM).

## Dependency compatibility

The SDK range is constrained to `litellm>=1.103.9,<1.104`, with an explicit
`filelock>=4.2.1,<5` security minimum. This keeps normal dependency resolution
compatible with the patched locking library. Later SDK releases currently cap
`filelock<4`, which conflicts with the fixes for CVE-2026-112940,
CVE-2026-112913 and CVE-2026-112907. Update this range after verifying that a newer
SDK accepts patched dependencies and passes the factory, tool, streaming and
embedding tests. No package compatibility constraints are overridden.

The locked service environment uses LiteLLM 1.103.9 and filelock 4.2.4. Its
`pip-audit` check reports no known vulnerabilities (the local editable project
is not a PyPI audit target). Ordinary tool calls work with the SDK's base
dependencies; no additional MCP runtime or proxy dependencies are installed.
A keyless transport test exercises the real SDK tool-call path.
