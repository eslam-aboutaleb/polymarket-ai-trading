"""Configuration settings for the Polymarket API"""

from enum import StrEnum
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AIBackend(StrEnum):
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
    # Envelope encryption backend for stored credentials:
    # "fernet" (local keyring, default) or "gcp_kms" (GCP Cloud
    # KMS wraps per-value data keys; requires google-cloud-kms).
    credential_encryption_backend: str = "fernet"
    # Full KMS key resource path, required when
    # credential_encryption_backend="gcp_kms", e.g.
    # projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY
    gcp_kms_key_name: str = ""
    # Aligned with the session policy: refresh tokens live 7 days
    # (default) to 90 days (keep_logged_in). 30 days covers the
    # default session with wide margin and most of the extended
    # session; a key-login user whose cached credentials age out
    # simply logs in again to re-derive them.
    credential_cache_ttl_seconds: int = 2592000  # 30 days
    require_redis_for_credentials: bool = True
    # Challenge cache (login flow): with more than one uvicorn
    # worker an in-process cache lets /login and /verify land on
    # different processes, causing intermittent "No pending
    # challenge found". Mirrors require_redis_for_credentials.
    require_redis_for_challenges: bool = True

    # Polymarket
    polymarket_private_key: str = ""
    polymarket_chain_id: int = 137  # Polygon Mainnet

    # OpenAI (for local fallback)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

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

    # Real-time price streaming (SSE)
    price_stream_max_sse_clients_per_user: int = 5
    price_stream_max_subscriptions: int = 50

    # Rate Limiting
    rate_limit_per_minute: int = 100

    # Server
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8002
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

    # Notifications — shared Telegram bot token for the alert center.
    # Secret: never expose to the frontend (no VITE_ variable).
    telegram_bot_token: str = ""

    # Trading correctness (plan 01): how long a submitted
    # GTC order may rest pending before the orphaned-GTC
    # reconciliation job cancels it (seconds).
    gtc_ttl_seconds: int = 86400

    # Paper trading (plan 08): simulated slippage bounds as a
    # fraction of price (0.003 = 0.3%). Depth-aware paper fills
    # are bounded to [slippage_sim_min, slippage_sim_max].
    slippage_sim_min: float = 0.003
    slippage_sim_max: float = 0.03

    # Latency arbitrage (plan 06). Paper-first: the engine runs
    # simulated fills for users with simulation_mode=true and
    # only submits real FOK orders when LATENCY_ARB_LIVE=true
    # AND the user's simulation_mode is false.
    latency_arb_live: bool = False
    # Minimum |P_model − p_market| gap to consider a candidate.
    latency_arb_edge_threshold: float = 0.03
    # Max notional per trade (USDC).
    latency_arb_max_notional: float = 50.0
    # Feed→order latency budget (ms): abort when exceeded.
    latency_arb_max_latency_ms: int = 1500
    # Engine cycle interval (seconds).
    latency_arb_cycle_seconds: int = 5
    # Per-strategy daily loss limit (USDC).
    latency_arb_daily_loss_limit: float = 20.0
    # Consecutive-loss circuit breaker: pause after N losses,
    # resume after the cooldown.
    latency_arb_circuit_breaker_losses: int = 3
    latency_arb_circuit_breaker_resume_seconds: int = 600
    # Allow trading the final seconds of a window.
    latency_arb_late_entry: bool = False
    latency_arb_late_entry_seconds: int = 10
    # Execution gates: minimum best bid/ask depth (shares) and
    # maximum quoted spread (fraction) — a wider spread is a
    # hard abort.
    latency_arb_min_depth: float = 25.0
    latency_arb_max_spread: float = 0.02
    # Symbols and window sizes the engine evaluates.
    latency_arb_symbols: str = "BTC,ETH,SOL"
    latency_arb_windows: str = "5,15,60"

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
            raise ValueError("JWT_SECRET_KEY is required and must not use a placeholder value.")
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

    @model_validator(mode="after")
    def validate_slippage_sim_bounds(self) -> "Settings":
        if self.slippage_sim_min < 0:
            raise ValueError("SLIPPAGE_SIM_MIN must be >= 0.")
        if self.slippage_sim_max < self.slippage_sim_min:
            raise ValueError("SLIPPAGE_SIM_MAX must be >= SLIPPAGE_SIM_MIN.")
        return self

    @model_validator(mode="after")
    def validate_latency_arb_settings(self) -> "Settings":
        if not 0.0 < self.latency_arb_edge_threshold < 1.0:
            raise ValueError("LATENCY_ARB_EDGE_THRESHOLD must be in (0, 1).")
        if self.latency_arb_max_notional <= 0:
            raise ValueError("LATENCY_ARB_MAX_NOTIONAL must be > 0.")
        if self.latency_arb_max_latency_ms < 1:
            raise ValueError("LATENCY_ARB_MAX_LATENCY_MS must be >= 1.")
        if self.latency_arb_cycle_seconds < 1:
            raise ValueError("LATENCY_ARB_CYCLE_SECONDS must be >= 1.")
        if self.latency_arb_daily_loss_limit < 0:
            raise ValueError("LATENCY_ARB_DAILY_LOSS_LIMIT must be >= 0.")
        if self.latency_arb_circuit_breaker_losses < 1:
            raise ValueError("LATENCY_ARB_CIRCUIT_BREAKER_LOSSES must be >= 1.")
        if self.latency_arb_circuit_breaker_resume_seconds < 1:
            raise ValueError("LATENCY_ARB_CIRCUIT_BREAKER_RESUME_SECONDS must be >= 1.")
        if self.latency_arb_late_entry_seconds < 1:
            raise ValueError("LATENCY_ARB_LATE_ENTRY_SECONDS must be >= 1.")
        if self.latency_arb_min_depth < 0:
            raise ValueError("LATENCY_ARB_MIN_DEPTH must be >= 0.")
        if not 0.0 < self.latency_arb_max_spread < 1.0:
            raise ValueError("LATENCY_ARB_MAX_SPREAD must be in (0, 1).")
        return self

    @field_validator(
        "trade_poll_max_concurrency",
        "trade_poll_http_max_connections",
        "trade_poll_http_keepalive_connections",
        "proxy_cache_ttl_seconds",
        "proxy_cache_max_entries",
        "in_memory_cache_max_entries",
        "price_stream_max_sse_clients_per_user",
        "price_stream_max_subscriptions",
        "gtc_ttl_seconds",
    )
    @classmethod
    def validate_positive_int_settings(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Performance tuning integer settings must be >= 1.")
        return value

    @model_validator(mode="after")
    def validate_debug_settings(self) -> "Settings":
        if self.debug_endpoints_enabled and not self.debug_log_hash_salt.strip():
            raise ValueError("DEBUG_LOG_HASH_SALT is required when DEBUG_ENDPOINTS_ENABLED=true.")
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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
