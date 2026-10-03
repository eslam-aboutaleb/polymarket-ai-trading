"""
ChromaDB-based RAG (Retrieval-Augmented Generation) for Polymarket markets.

Inspired by Polymarket/agents chroma.py — vectorizes active markets/events
into a local ChromaDB collection, then uses similarity search to find the
most relevant opportunities for any given query.

Features:
- Index all active markets with metadata (prices, volume, liquidity)
- Multi-query RAG: generate N paraphrased queries for better recall
- Semantic filtering for opportunity discovery
- TTL-based auto-refresh of the index
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from src.config import get_settings

logger = logging.getLogger(__name__)

# Lazy imports — these are optional heavy deps
_chromadb = None
_OpenAIEmbeddings = None


def _ensure_imports():
    """Lazy-import chromadb and embeddings to avoid startup cost if RAG is disabled."""
    global _chromadb, _OpenAIEmbeddings
    if _chromadb is None:
        try:
            import chromadb as _chromadb_mod

            _chromadb = _chromadb_mod
        except ImportError:
            logger.warning("chromadb not installed — RAG features disabled")
            return False
    if _OpenAIEmbeddings is None:
        try:
            from langchain_openai import OpenAIEmbeddings as _OAI

            _OpenAIEmbeddings = _OAI
        except ImportError:
            logger.warning("langchain-openai not installed — RAG features disabled")
            return False
    return True


class MarketRAGService:
    """
    Manages a ChromaDB vector store of Polymarket markets for
    semantic similarity search and opportunity discovery.
    """

    _instance: MarketRAGService | None = None
    _last_index_time: float = 0
    _index_ttl_seconds: int = 15 * 60  # 15 minutes

    def __init__(self):
        self.settings = get_settings()
        self._collection = None
        self._embedding_fn = None
        self._chroma_client = None
        self._initialized = False

    @classmethod
    def get_instance(cls) -> MarketRAGService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _init_chroma(self) -> bool:
        """Initialize ChromaDB client and collection."""
        if self._initialized:
            return True
        if not _ensure_imports():
            return False

        try:
            persist_dir = self.settings.chroma_persist_dir
            Path(persist_dir).mkdir(parents=True, exist_ok=True)

            self._chroma_client = _chromadb.PersistentClient(path=persist_dir)
            self._collection = self._chroma_client.get_or_create_collection(
                name=self.settings.chroma_collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            self._embedding_fn = _OpenAIEmbeddings(
                model=self.settings.rag_embedding_model,
                openai_api_key=self.settings.openai_api_key,
            )
            self._initialized = True
            logger.info(
                "ChromaDB initialized: collection=%s, persist=%s",
                self.settings.chroma_collection_name,
                persist_dir,
            )
            return True
        except Exception as e:
            logger.error("Failed to initialize ChromaDB: %s", e)
            return False

    def index_markets(self, markets: list[dict[str, Any]], force: bool = False) -> int:
        """
        Index a list of market dicts into ChromaDB.

        Args:
            markets: List of market dicts with keys like question, description,
                     outcomePrices, volume, liquidity, conditionId, etc.
            force: Re-index even if TTL hasn't expired.

        Returns:
            Number of markets indexed.
        """
        if not self.settings.rag_enabled:
            return 0

        now = time.time()
        if not force and (now - self._last_index_time) < self._index_ttl_seconds:
            logger.debug("RAG index still fresh (%.0fs old), skipping", now - self._last_index_time)
            return 0

        if not self._init_chroma():
            return 0

        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []
        ids: list[str] = []

        for m in markets:
            cid = str(m.get("condition_id") or m.get("conditionId") or m.get("id", ""))
            if not cid:
                continue

            question = m.get("question", "")
            description = m.get("description", "")
            doc_text = f"{question}\n{description}".strip()
            if not doc_text:
                continue

            # Parse outcome prices safely
            prices = m.get("outcomePrices", [])
            try:
                yes_p = float(prices[0]) if prices else 0.5
                no_p = float(prices[1]) if len(prices) > 1 else 1.0 - yes_p
            except (ValueError, TypeError, IndexError):
                yes_p, no_p = 0.5, 0.5

            meta = {
                "condition_id": cid,
                "question": question[:500],
                "yes_price": yes_p,
                "no_price": no_p,
                "volume": float(m.get("volume", 0) or 0),
                "volume_24h": float(m.get("volume24hr", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
                "end_date": str(m.get("endDate", "") or ""),
                "slug": str(m.get("slug", "") or ""),
                "active": bool(m.get("active", True)),
            }

            documents.append(doc_text)
            metadatas.append(meta)
            ids.append(cid)

        if not documents:
            return 0

        try:
            # Upsert to handle re-indexing gracefully
            # ChromaDB requires embeddings or we let it auto-embed
            embeddings = self._embedding_fn.embed_documents(documents)
            self._collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
            )
            self._last_index_time = time.time()
            logger.info("Indexed %d markets into ChromaDB", len(documents))
            return len(documents)
        except Exception as e:
            logger.error("Failed to index markets in ChromaDB: %s", e)
            return 0

    def search(
        self,
        query: str,
        n_results: int = 10,
        min_volume: float = 0,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Search for markets semantically similar to a query.

        Args:
            query: Natural language search query.
            n_results: Max results to return.
            min_volume: Minimum 24h volume filter.
            active_only: Only return active markets.

        Returns:
            List of dicts with keys: condition_id, question, score, metadata.
        """
        if not self._init_chroma():
            return []

        try:
            query_embedding = self._embedding_fn.embed_query(query)

            where_filter = {}
            if active_only:
                where_filter["active"] = True

            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=min(n_results * 2, 50),  # Over-fetch for post-filtering
                where=where_filter if where_filter else None,
                include=["documents", "metadatas", "distances"],
            )

            if not results or not results.get("ids") or not results["ids"][0]:
                return []

            output = []
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else 1.0
                score = 1.0 - distance  # cosine distance → similarity

                # Post-filter by volume
                if min_volume > 0 and float(meta.get("volume_24h", 0)) < min_volume:
                    continue

                output.append(
                    {
                        "condition_id": doc_id,
                        "question": meta.get("question", ""),
                        "score": round(score, 4),
                        "distance": round(distance, 4),
                        "metadata": meta,
                        "document": results["documents"][0][i] if results["documents"] else "",
                    }
                )

            return output[:n_results]
        except Exception as e:
            logger.error("ChromaDB search failed: %s", e)
            return []

    async def multi_query_search(
        self,
        query: str,
        llm,
        n_results: int = 10,
        min_volume: float = 0,
    ) -> list[dict[str, Any]]:
        """
        Multi-query RAG: generate N paraphrased queries using LLM,
        then union the results for better recall.

        Inspired by Polymarket/agents multi-query technique.
        """
        from src.analysis_chain import PromptManager

        prompts = PromptManager()

        # Generate alternative queries
        try:
            expansion_prompt = prompts.get("research", "multi_query_expansion")
            expansion_text = expansion_prompt.format(question=query)
            response = await llm.ainvoke([HumanMessage(content=expansion_text)])
            alt_queries = [
                line.strip() for line in response.content.strip().split("\n") if line.strip()
            ][: self.settings.rag_multi_query_count]
        except Exception as e:
            logger.warning("Multi-query expansion failed: %s", e)
            alt_queries = []

        # Search with original + alternative queries
        all_queries = [query] + alt_queries
        seen_ids = set()
        merged = []

        for q in all_queries:
            results = self.search(q, n_results=n_results, min_volume=min_volume)
            for r in results:
                cid = r["condition_id"]
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    merged.append(r)

        # Sort by best score
        merged.sort(key=lambda x: x["score"], reverse=True)
        return merged[:n_results]

    def get_collection_size(self) -> int:
        """Return the number of documents in the collection."""
        if not self._init_chroma():
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0

    def clear(self):
        """Clear the entire collection."""
        if self._chroma_client:
            try:
                self._chroma_client.delete_collection(self.settings.chroma_collection_name)
                self._collection = self._chroma_client.get_or_create_collection(
                    name=self.settings.chroma_collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
                self._last_index_time = 0
                logger.info("Cleared ChromaDB collection")
            except Exception as e:
                logger.error("Failed to clear ChromaDB: %s", e)
