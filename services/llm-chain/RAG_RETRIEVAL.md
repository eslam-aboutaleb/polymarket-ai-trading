# Evidence-grounded RAG read path (issue #39)

Off by default. Set `RAG_ENABLED=true` and `RAG_RETRIEVAL_ENABLED=true` to turn it on. With either
flag off, `AnalyzeMarket` and `DiscoverBestTrade` make no embedding, vector-store or extra LLM call
and add no `retrieval_*` metadata.

The index is the existing local SQLite vector store (`market_rag.py`). The issue text says Chroma;
the code no longer uses Chroma, so "seeded Chroma fixture" is read as "seeded vector store".

## Request and hit schema
`RAGRetrievalRequest(query, canonical_condition_id, token_id, as_of, min_volume, active_only,
max_results, user_scope, identity_mode)` and `RAGHit` (condition_id, token_id, event_id, title,
retrieved_at, observed_at, indexed_at, embedding_version, schema_version, similarity, venue,
source_ref, market_state, quality, relation, document_hash, indexed_yes_price,
indexed_price_observed_at, price_is_live=false, price_label).

`AnalyzeMarketRequest` carries optional `condition_id`, `token_id`, `as_of` (proto fields 11 to 13).

## Behaviour
* Identity: `related` mode never returns the target market and rejects a same-condition hit with a
  different token (`wrong_outcome`). `same_market` mode accepts only the canonical condition and token.
* As-of: a row is visible only if `indexed_at` and `observed_at` are at or before the decision time
  (`as_of`, else now). Rows without the `rag-market-v2` schema cannot prove provenance and are omitted.
  Limit: the index upserts one row per market, so a re-indexed market shows its newest `indexed_at`
  and is omitted from older as-of reads instead of returning its older version.
* Filters with counted omissions: stale, closed, unsupported venue, out of account scope, low
  similarity, low volume, ambiguous identity.
* Prices: indexed prices are labelled stale snapshots. `discover_best_trade` merges only markets that
  already carry live prices from the caller's list, deduplicated by condition id, ordered by
  (-similarity, condition_id), capped at 2 extra candidates.
* Failure: `status` is `disabled`, `ok`, `no_index` or `unavailable`. Provider errors report the
  exception class only. A circuit breaker, request deadline, bounded 2-thread executor, result cap and
  TTL cache bound cost. Retrieved text is sanitised and labelled untrusted data.
* Response metadata keys: `retrieval_status`, `retrieval_source_count`, `retrieval_hit_ids`,
  `retrieval_index_age_seconds`, `retrieval_omitted`, `retrieval_omitted_stale`,
  `retrieval_embedding_version`, `retrieval_no_index_reason`, `retrieval_provider_error`,
  `retrieval_hits_json`, `retrieval_as_of`.
* The read path has no order, signing or CLOB surface.

## Offline evaluation
`PYTHONPATH=../shared:. python -m src.rag_eval` (from `services/llm-chain`). It is a pre-registered,
deterministic, synthetic policy evaluation with hashing embeddings. It does not measure production
embedding quality and makes no profitability claim. Result at commit time (8 queries, 48 markets, k=3):

| system | recall@3 | precision@3 | unusable-hit rate |
| --- | --- | --- | --- |
| no RAG | 0.0 | 0.0 | 0.0 |
| lexical overlap, no filters | 0.0 | 0.0 | 1.0 |
| unfiltered similarity search | 0.0 | 0.0 | 1.0 |
| read path | 0.1875 | 0.125 | 0.0 |

False source attribution 0, p95 latency 2.5 ms on the fake embedder. Not measured: real embeddings,
a real held-out market corpus, paired with-vs-without-RAG forecast scores, wrong-rule rate, token
cost. These need production index data and a live provider.
