"""Configuration for CLI Agent service."""
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    """Service settings loaded from environment."""
    
    # Service
    service_name: str = "cli-agent-service"
    service_version: str = "0.1.0"
    grpc_port: int = 50052
    
    # GitHub Models API (OpenAI-compatible endpoint using GITHUB_TOKEN)
    github_token: str = ""
    github_models_endpoint: str = "https://models.inference.ai.azure.com"
    github_model: str = "gpt-4o-mini"
    github_model_temperature: float = 0.3
    github_model_max_tokens: int = 2048
    copilot_timeout: int = 120  # seconds
    
    # Fallback: OpenAI direct (if GITHUB_TOKEN is missing)
    openai_api_key: str = ""
    
    # MCP Configuration
    mcp_config_path: str = "/app/mcp-config.json"
    
    # Polymarket MCP
    polymarket_private_key: str = ""
    polymarket_chain_id: int = 137
    
    # Prompts
    prompts_path: str = "/app/prompts/prompts.json"
    
    # Search API Keys (for MCPs)
    brave_api_key: str = ""
    
    # Logging
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
