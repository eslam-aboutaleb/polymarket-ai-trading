"""Pre-registered offline evaluation for the RAG read path (issue #39).

The corpus, held-out queries and labels are generated deterministically from
fixed lists below and never tuned against the retrieval code. The embedding
model is a deterministic hashing bag-of-words stand-in, so the numbers measure
the *retrieval policy* (identity, as-of, staleness, venue, scope filters)
against a lexical baseline and unfiltered similarity search. They are NOT a
measurement of production embedding quality, and make no profitability claim.
Run ``python -m src.rag_eval`` from ``services/llm-chain`` to print the report.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from src import market_rag
from src.config import Settings
from src.market_rag import MarketRAGService, RAGRetrievalRequest

DIMS = 128
STOP = {"will", "the", "be", "a", "of", "on", "in", "by", "to", "is", "at", "end"}
ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
THRESHOLDS = [("above", 100), ("above", 120), ("below", 80)]
NOW = 2_000_000_000.0
DAY = 86_400.0


def vector(text: str) -> list[float]:
    out = [0.0] * DIMS
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word not in STOP:
            out[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMS] += 1.0
    return out


class HashEmbeddings:
    def embed_documents(self, documents):
        return [vector(d) for d in documents]

    def embed_query(self, query):
        return vector(query)


@dataclass(frozen=True)
class LabeledQuery:
    query: str
    target: str
    relevant: frozenset[str]  # sibling markets on the same asset (held-out labels)
    unusable: frozenset[str]  # self, stale, closed, other-venue rows


def build_corpus() -> tuple[list[dict[str, Any]], list[LabeledQuery]]:
    markets: list[dict[str, Any]] = []
    queries: list[LabeledQuery] = []
    for asset in ASSETS:
        ids = []
        for direction, level in THRESHOLDS:
            cid = f"{asset}-{direction}-{level}"
            ids.append(cid)
            markets.append(
                {
                    "condition_id": cid,
                    "question": f"Will {asset} be {direction} {level}k on Dec 31",
                    "clobTokenIds": [f"{cid}-yes", f"{cid}-no"],
                    "volume24hr": 100,
                    "endDate": "2099-01-01T00:00:00Z",
                    "_age_days": 0.1,
                }
            )
        junk = {
            f"{asset}-stale": {"_age_days": 30},
            f"{asset}-closed": {"active": False, "_age_days": 0.1},
            f"{asset}-kalshi": {"venue": "kalshi", "_age_days": 0.1},
        }
        for cid, extra in junk.items():
            markets.append(
                {
                    "condition_id": cid,
                    "question": f"Will {asset} be above 100k on Dec 31 {cid.split('-')[1]} copy",
                    "clobTokenIds": [f"{cid}-yes", f"{cid}-no"],
                    "volume24hr": 100,
                    "endDate": "2099-01-01T00:00:00Z",
                    **extra,
                }
            )
        target = ids[0]
        # Held-out phrasing: differs from the indexed question wording.
        queries.append(
            LabeledQuery(
                query=f"{asset} price above 100k on Dec 31 forecast",
                target=target,
                relevant=frozenset(ids[1:]),
                unusable=frozenset([target, *junk]),
            )
        )
    return markets, queries


def lexical_rank(query: str, markets: list[dict[str, Any]], k: int) -> list[str]:
    """Baseline: token-overlap ranking over the same corpus, no filters."""
    q = set(re.findall(r"[a-z0-9]+", query.lower())) - STOP

    def score(m: dict[str, Any]) -> tuple[float, str]:
        d = set(re.findall(r"[a-z0-9]+", m["question"].lower())) - STOP
        return (-(len(q & d) / len(q | d) if q | d else 0.0), m["condition_id"])

    return [m["condition_id"] for m in sorted(markets, key=score)[:k]]


def _metrics(returned: dict[str, list[str]], queries: list[LabeledQuery], k: int) -> dict:
    recalls, precisions, unusable, total = [], [], 0, 0
    for q in queries:
        ids = returned[q.query][:k]
        got = len(set(ids) & q.relevant)
        recalls.append(got / len(q.relevant))
        precisions.append(got / len(ids) if ids else 0.0)
        unusable += sum(1 for i in ids if i in q.unusable)
        total += len(ids)
    return {
        f"recall@{k}": round(statistics.mean(recalls), 4),
        f"precision@{k}": round(statistics.mean(precisions), 4),
        "unusable_hit_rate": round(unusable / total, 4) if total else 0.0,
    }


def run(k: int = 3) -> dict[str, Any]:
    markets, queries = build_corpus()
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            _env_file=None,
            rag_enabled=True,
            rag_retrieval_enabled=True,
            chroma_persist_dir=tmp,
            chroma_collection_name="eval",
            rag_embedding_model="eval",
            gateway_base="https://gateway.example/v1",
            gateway_key="eval",
            rag_retrieval_cache_ttl_seconds=0,
            rag_retrieval_min_similarity=0.3,
        )
        original = (market_rag.create_embeddings, market_rag.get_settings)
        market_rag.create_embeddings = lambda _: HashEmbeddings()
        market_rag.get_settings = lambda: settings
        try:
            service = MarketRAGService()
            for m in markets:
                age = m.pop("_age_days")
                service.index_markets([m], force=True, indexed_at=NOW - age * DAY)

            baseline = {q.query: lexical_rank(q.query, markets, k) for q in queries}
            unfiltered = {
                q.query: [
                    r["condition_id"]
                    for r in service.search(q.query, n_results=k, active_only=False)
                ]
                for q in queries
            }
            retrieved: dict[str, list[str]] = {}
            latencies = []
            misattributed = 0
            for q in queries:
                start = time.perf_counter()
                result = service.retrieve(
                    RAGRetrievalRequest(
                        query=q.query, canonical_condition_id=q.target, max_results=k
                    ),
                    now=NOW,
                )
                latencies.append((time.perf_counter() - start) * 1000)
                retrieved[q.query] = [h.condition_id for h in result.hits]
                misattributed += sum(
                    1 for h in result.hits if h.source_ref != f"polymarket:market:{h.condition_id}"
                )
        finally:
            market_rag.create_embeddings, market_rag.get_settings = original
    latencies.sort()
    return {
        "kind": "synthetic policy evaluation; hashing embeddings; not production embedding quality",
        "queries": len(queries),
        "corpus_size": len(markets),
        "k": k,
        "no_rag_baseline": {f"recall@{k}": 0.0, f"precision@{k}": 0.0, "unusable_hit_rate": 0.0},
        "lexical_baseline": _metrics(baseline, queries, k),
        "unfiltered_similarity_search": _metrics(unfiltered, queries, k),
        "read_path": _metrics(retrieved, queries, k),
        "false_source_attribution": misattributed,
        "latency_ms_p95": round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 2),
        "approx_query_tokens": round(statistics.mean(len(q.query) / 4 for q in queries), 1),
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(), indent=2) + "\n")
