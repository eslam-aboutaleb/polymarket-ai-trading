# Portable model gateway configuration

The `llm-chain` service supports `LLM_PROVIDER=litellm` through its existing
chat factories and the shared `polymarket_settings` adapter. Legacy providers
remain available. No browser receives a virtual key. The separate `cli-agent`
service continues to serve explicit GitHub Models selections; choose `litellm`
or leave the provider preference unset to use the configured LLM service.

Configure the service environment (or your secret manager):

```dotenv
LLM_PROVIDER=litellm
LITELLM_API_BASE=https://gateway.example/v1
LITELLM_API_KEY=<virtual-key>
LITELLM_MODEL=openai/<chat-alias>
LITELLM_EMBEDDING_MODEL=openai/<embedding-alias>
LITELLM_EMBEDDING_DIMENSIONS=1536
```

`openai/` is LiteLLM's protocol selector for OpenAI-compatible gateways. LiteLLM
removes the prefix before sending the model alias. Use native identifiers such
as `anthropic/<model>` for direct providers instead. The application contains no
gateway address, alias mapping, credential, or provider routing heuristic.

| Setting | Behavior |
| --- | --- |
| `LITELLM_API_BASE`, `LITELLM_API_KEY` | Optional shared connection. When absent or empty, omitted from SDK arguments so provider-native authentication and endpoint lookup remain available. |
| `LITELLM_MODEL` | Chat default; falls back to legacy `LLM_MODEL`. |
| `LITELLM_EMBEDDING_MODEL` | Independent embedding default; falls back to legacy `RAG_EMBEDDING_MODEL`. Setting it enables LiteLLM embeddings even when chat uses a legacy provider. |
| `LITELLM_EMBEDDING_API_KEY` | Unset inherits the shared key. A supplied key overrides it; empty defers to native authentication. |
| `LITELLM_EMBEDDING_API_BASE` | Unset inherits the shared endpoint. Explicitly empty cancels inheritance and uses the provider's default/native endpoint. |
| `LITELLM_EMBEDDING_DIMENSIONS` | Optional positive dimension count, including for custom aliases; overrides legacy `RAG_EMBEDDING_DIMENSIONS`. Unset leaves dimensions to the provider. |

All three chat factories use the same adapter, including sync/async invocation,
streaming, callbacks, structured output and tool binding. Explicit request models
win over configured task-tier models, which win over `LITELLM_MODEL`, then
`LLM_MODEL`. Task-tier model overrides apply only when `LLM_TIERING_ENABLED=true`.
OpenAI-specific tier defaults and automatic reasoning-effort parameters are not
sent to custom gateway aliases. Sampling and token limits still follow the
existing tier configuration. Configure aliases accepted by your gateway for any
explicit request or tier override; the gateway enforces its allowed model list.

The Compose files forward optional gateway variables without substituting empty
strings. This preserves the difference between unset and empty embedding
overrides. Do not populate every optional variable with `""` in a secret manager.
Keep keys in the LLM service environment and out of `VITE_*`, frontend files,
request payloads, images and committed environment files. Endpoint validation
allows HTTP(S) private/local providers and keeps default TLS verification;
userinfo, query strings, fragments, invalid ports and unsupported protocols are
rejected. Credentials are masked or excluded in settings/client representations,
serialization and validation errors. Provider failures cross the adapter as
sanitized errors, without provider response bodies or request headers.

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
No pull-request job references production gateway secrets. Repository secrets are
stored only after successful authenticated gateway and application verification;
adding secrets does not automatically inject them into workflows.

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
