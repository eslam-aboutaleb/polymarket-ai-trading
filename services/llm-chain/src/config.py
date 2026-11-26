"""Configuration for LLM Chain service."""

from functools import lru_cache

from polymarket_settings import SharedLLMSettings
from pydantic import Field
from pydantic_settings import SettingsConfigDict


class Settings(SharedLLMSettings):
    """Service settings loaded from environment.

    Inherits the shared LLM configuration (provider, model,
    temperature, max_tokens, per-task tiering) from
    ``polymarket_settings.SharedLLMSettings``; this class keeps only
    the llm-chain-specific fields.
    """

    # Service
    service_name: str = "llm-chain-service"
    service_version: str = "0.2.0"
    grpc_port: int = 50051

    # Provider API Keys (only the selected provider's key is required)
    openai_api_key: str = Field(default="", repr=False, exclude=True)
    anthropic_api_key: str = Field(default="", repr=False, exclude=True)
    google_api_key: str = Field(default="", repr=False, exclude=True)
    groq_api_key: str = Field(default="", repr=False, exclude=True)

    # Ollama (local) settings
    ollama_base_url: str = "http://localhost:11434"

    # Prompts
    prompts_path: str = "/app/prompts/prompts.json"
    prompt_contracts_root: str = ""

    # SQLite RAG store settings. CHROMA_* names remain supported for existing deployments.
    chroma_persist_dir: str = "/app/data/chroma"
    chroma_collection_name: str = "polymarket_markets"
    rag_embedding_model: str = "text-embedding-3-small"
    rag_enabled: bool = True
    rag_multi_query_count: int = 5

    # Evidence-grounded RAG *read* path (issue #39). Off by default: with the
    # flag off no embedding, vector-store or LLM call is made on the read route.
    rag_retrieval_enabled: bool = False
    rag_retrieval_deadline_seconds: float = 5.0
    rag_retrieval_max_results: int = 10
    rag_retrieval_min_similarity: float = 0.3
    rag_retrieval_max_index_age_seconds: float = 24 * 3600.0
    rag_retrieval_cache_ttl_seconds: float = 60.0
    rag_retrieval_cache_size: int = 128
    rag_retrieval_query_max_chars: int = 1000
    rag_retrieval_breaker_threshold: int = 3
    rag_retrieval_breaker_cooldown_seconds: float = 60.0

    # Tavily premium search (optional)
    tavily_api_key: str = Field(default="", repr=False, exclude=True)
    tavily_enabled: bool = False

    # NewsAPI (optional)
    newsapi_api_key: str = Field(default="", repr=False, exclude=True)
    newsapi_enabled: bool = False

    # Binance enrichment is gated by the BINANCE_SKILLS_ENABLED environment
    # variable read inside the shared research-MCP client, so it applies to
    # this service and cli-agent with a single switch. (A mirrored settings
    # field used to live here; it was dead — nothing ever read it.)

    # Logging
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
