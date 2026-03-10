"""Configuration for LLM Chain service."""
from pydantic_settings import BaseSettings
from functools import lru_cache
from enum import Enum


class LLMProvider(str, Enum):
    """Supported LLM providers."""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    GROQ = "groq"
    OLLAMA = "ollama"  # Local models


class Settings(BaseSettings):
    """Service settings loaded from environment."""
    
    # Service
    service_name: str = "llm-chain-service"
    service_version: str = "0.2.0"
    grpc_port: int = 50051
    
    # LLM Configuration (provider-agnostic)
    llm_provider: LLMProvider = LLMProvider.OPENAI
    llm_model: str = "gpt-4-turbo-preview"  # Model name varies by provider
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096
    
    # Provider API Keys (only the selected provider's key is required)
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    groq_api_key: str = ""
    
    # Ollama (local) settings
    ollama_base_url: str = "http://localhost:11434"
    
    # Prompts
    prompts_path: str = "/app/prompts/prompts.json"
    
    # ChromaDB / RAG settings
    chroma_persist_dir: str = "/app/data/chroma"
    chroma_collection_name: str = "polymarket_markets"
    rag_embedding_model: str = "text-embedding-3-small"
    rag_enabled: bool = True
    rag_multi_query_count: int = 5
    
    # Tavily premium search (optional)
    tavily_api_key: str = ""
    tavily_enabled: bool = False
    
    # NewsAPI (optional)
    newsapi_api_key: str = ""
    newsapi_enabled: bool = False
    
    # Binance Skills Hub (public APIs, no auth required)
    binance_skills_enabled: bool = True
    
    # Logging
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
