"""Configuration settings for the Polymarket API"""
from functools import lru_cache
from enum import Enum
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class AIBackend(str, Enum):
    """Available AI backend options"""
    LLM_CHAIN = "llm_chain"  # LangChain + OpenAI
    CLI_AGENT = "cli_agent"  # GitHub Copilot CLI


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # API Configuration
    api_title: str = "Polymarket AI Trading API"
    api_version: str = "0.1.0"
    api_description: str = "AI-powered trading automation for Polymarket"
    
    # Database
    database_url: str = "postgresql://user:password@localhost:5432/polymarket"
    
    # Redis (for caching and rate limiting)
    redis_url: str = ""
    
    # Authentication
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    refresh_token_hash_secret: str = ""

    # Credential encryption / cache
    encryption_master_keys: str = ""
    credential_cache_ttl_seconds: int = 3600
    require_redis_for_credentials: bool = True
    
    # Polymarket
    polymarket_private_key: str = ""
    polymarket_chain_id: int = 137  # Polygon Mainnet
    
    # OpenAI (for local fallback)
    openai_api_key: str = ""
    openai_model: str = "gpt-4-turbo-preview"

    # SMTP notifications
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    
    # Prompts
    prompts_path: str = "prompts/prompts.json"
    
    # Trading
    execution_mode: str = "approval"  # auto or approval
    max_position_size: float = 1000.0  # Max USDC per trade
    stop_loss_check_interval_seconds: int = 10
    trade_poll_max_concurrency: int = 50
    trade_poll_http_max_connections: int = 200
    trade_poll_http_keepalive_connections: int = 50
    proxy_cache_ttl_seconds: int = 86400
    proxy_cache_max_entries: int = 10000
    in_memory_cache_max_entries: int = 10000
    
    # Rate Limiting
    rate_limit_per_minute: int = 100
    
    # Server
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = True
    cors_allowed_origins: str = "http://localhost:5173,http://localhost:3000"

    # Debug / observability hardening
    debug_endpoints_enabled: bool = False
    debug_log_hash_salt: str = ""
    
    # Admin — comma-separated wallet addresses that get is_admin=True
    admin_wallets: str = ""
    admin_sync_allow_empty: bool = False
    
    # AI Services (gRPC)
    default_ai_backend: AIBackend = AIBackend.LLM_CHAIN
    llm_chain_grpc_host: str = "localhost"
    llm_chain_grpc_port: int = 50051
    cli_agent_grpc_host: str = "localhost"
    cli_agent_grpc_port: int = 50052
    grpc_timeout: int = 60  # seconds

    # Binance Skills Hub (public APIs, no auth required)
    binance_skills_enabled: bool = True

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret_key(cls, value: str) -> str:
        secret = (value or "").strip()
        insecure_values = {
            "",
            "change-me-in-production",
            "changeme",
            "default",
            "secret",
        }
        if secret.lower() in insecure_values:
            raise ValueError(
                "JWT_SECRET_KEY is required and must not use a placeholder value."
            )
        if len(secret) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters.")
        return secret

    @field_validator("credential_cache_ttl_seconds")
    @classmethod
    def validate_credential_ttl(cls, value: int) -> int:
        if value < 60:
            raise ValueError("CREDENTIAL_CACHE_TTL_SECONDS must be >= 60.")
        return value

    @field_validator("stop_loss_check_interval_seconds")
    @classmethod
    def validate_stop_loss_interval(cls, value: int) -> int:
        if value < 1:
            raise ValueError("STOP_LOSS_CHECK_INTERVAL_SECONDS must be >= 1.")
        return value

    @field_validator(
        "trade_poll_max_concurrency",
        "trade_poll_http_max_connections",
        "trade_poll_http_keepalive_connections",
        "proxy_cache_ttl_seconds",
        "proxy_cache_max_entries",
        "in_memory_cache_max_entries",
    )
    @classmethod
    def validate_positive_int_settings(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Performance tuning integer settings must be >= 1.")
        return value

    @model_validator(mode="after")
    def validate_debug_settings(self) -> "Settings":
        if self.debug_endpoints_enabled and not self.debug_log_hash_salt.strip():
            raise ValueError(
                "DEBUG_LOG_HASH_SALT is required when DEBUG_ENDPOINTS_ENABLED=true."
            )
        return self

    @property
    def is_local_environment(self) -> bool:
        return self.environment.lower() in {"dev", "development", "local", "test"}

    @property
    def debug_endpoints_active(self) -> bool:
        """Enable debug APIs by default in local/dev, explicit opt-in elsewhere."""
        return self.debug_endpoints_enabled or self.is_local_environment

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]
    
    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
