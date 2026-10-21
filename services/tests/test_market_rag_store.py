"""Exercise persistent semantic indexing without external vector-store services."""

import sys
from pathlib import Path
from types import SimpleNamespace

SERVICE_ROOT = Path(__file__).resolve().parents[1] / "llm-chain"
sys.path.insert(0, str(SERVICE_ROOT))
sys.path.insert(0, str(SERVICE_ROOT.parent / "shared"))

from src import market_rag  # noqa: E402


class FakeEmbeddings:
    def __init__(self, **_kwargs):
        pass

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "rates" in doc else [0.0, 1.0] for doc in documents]

    def embed_query(self, query: str) -> list[float]:
        return [1.0, 0.0] if "rates" in query else [0.0, 1.0]


def test_sqlite_rag_store_upserts_filters_searches_and_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(market_rag, "_OpenAIEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(
        market_rag,
        "get_settings",
        lambda: SimpleNamespace(
            rag_enabled=True,
            chroma_persist_dir=str(tmp_path),
            chroma_collection_name="markets",
            rag_embedding_model="test",
            openai_api_key="test",
        ),
    )
    monkeypatch.setattr(market_rag.MarketRAGService, "_instance", None)
    service = market_rag.MarketRAGService.get_instance()

    markets = [
        {
            "condition_id": "rates",
            "question": "Will rates change?",
            "description": "rates decision",
            "volume24hr": 50,
            "active": True,
        },
        {
            "condition_id": "election",
            "question": "Who will win?",
            "description": "election outcome",
            "volume24hr": 2,
            "active": True,
        },
        {
            "condition_id": "closed",
            "question": "Past rates decision",
            "description": "rates history",
            "volume24hr": 200,
            "active": False,
        },
    ]

    assert service.index_markets(markets, force=True) == 3
    assert service.index_markets(markets[:1], force=True) == 1
    assert service.get_collection_size() == 3

    results = service.search("rates decision", min_volume=10)

    assert [item["condition_id"] for item in results] == ["rates"]
    assert results[0]["score"] == 1.0
    assert results[0]["document"] == "Will rates change?\nrates decision"

    service.clear()

    assert service.get_collection_size() == 0
