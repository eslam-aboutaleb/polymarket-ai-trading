"""User settings model for preferences like AI backend selection"""
from sqlalchemy import CheckConstraint, Column, String, DateTime, Integer, ForeignKey, Boolean, Float
from datetime import datetime
from app.utils.time import utc_now
from enum import Enum as PyEnum

from app.models.base import Base


class AIBackendType(str, PyEnum):
    """Available AI backend options (legacy - maps to service)"""
    LLM_CHAIN = "llm_chain"
    CLI_AGENT = "cli_agent"


class LLMProviderType(str, PyEnum):
    """
    Available LLM providers (new - specific provider selection).
    
    These map to:
    - LLM-chain service (port 50051): OPENAI, ANTHROPIC, GOOGLE, GROQ, OLLAMA
    - CLI-agent service (port 50052): GITHUB_MODELS
    """
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    GROQ = "groq"
    OLLAMA = "ollama"
    GITHUB_MODELS = "github_models"


class RiskMode(str, PyEnum):
    """Copy-trading risk management mode"""
    MAX_POSITION_DAILY_LOSS = "max_position_daily_loss"
    PERCENTAGE_MIRROR = "percentage_mirror"
    FIXED_AMOUNT = "fixed_amount"


class InverseBotSizeMode(str, PyEnum):
    """Inverse-bot sizing mode."""
    FULL_NOTIONAL = "full_notional"
    FIXED_AMOUNT = "fixed_amount"


class UserSettings(Base):
    """User settings for preferences"""
    __tablename__ = "user_settings"
    __table_args__ = (
        CheckConstraint(
            "(mirror_percentage IS NULL OR (mirror_percentage >= 0 AND mirror_percentage <= 100))",
            name="ck_user_settings_mirror_percentage_range",
        ),
        CheckConstraint(
            "(inverse_bot_confidence_threshold >= 0 AND inverse_bot_confidence_threshold <= 100)",
            name="ck_user_settings_inverse_confidence_range",
        ),
        CheckConstraint(
            "(max_position_size IS NULL OR max_position_size >= 0)",
            name="ck_user_settings_max_position_size_non_negative",
        ),
        CheckConstraint(
            "(daily_loss_limit IS NULL OR daily_loss_limit >= 0)",
            name="ck_user_settings_daily_loss_limit_non_negative",
        ),
        CheckConstraint(
            "(fixed_trade_amount IS NULL OR fixed_trade_amount >= 0)",
            name="ck_user_settings_fixed_trade_amount_non_negative",
        ),
        CheckConstraint(
            "(inverse_bot_fixed_amount >= 0)",
            name="ck_user_settings_inverse_fixed_amount_non_negative",
        ),
        CheckConstraint(
            "(inverse_bot_cooldown_minutes >= 0)",
            name="ck_user_settings_inverse_cooldown_non_negative",
        ),
        CheckConstraint(
            "(inverse_bot_max_reversals_per_day >= 0)",
            name="ck_user_settings_inverse_max_reversals_non_negative",
        ),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    
    # AI Backend preference (legacy - service-level selection)
    ai_backend = Column(
        String(20),
        default=AIBackendType.LLM_CHAIN.value,
        nullable=False
    )
    
    # LLM Provider preference (new - specific provider selection)
    # When set, this takes precedence over ai_backend for provider selection
    preferred_llm_provider = Column(
        String(30),
        nullable=True,  # NULL = use ai_backend for backwards compatibility
        default=None,
    )
    
    # Preferred model for the selected provider (optional)
    preferred_llm_model = Column(
        String(100),
        nullable=True,  # NULL = use provider default
        default=None,
    )
    
    # ── Copy-Trading Settings ──────────────────────────────────
    copy_trading_enabled = Column(Boolean, default=False, nullable=False)
    risk_mode = Column(
        String(30),
        default=RiskMode.MAX_POSITION_DAILY_LOSS.value,
        nullable=False,
    )
    # Max-position + daily-loss mode
    max_position_size = Column(Float, default=100.0)   # USDC cap per trade
    daily_loss_limit = Column(Float, default=500.0)     # USDC daily stop
    # Percentage-mirror mode
    mirror_percentage = Column(Float, default=10.0)     # 0-100
    # Fixed-amount mode
    fixed_trade_amount = Column(Float, default=50.0)    # USDC per copied trade
    # AI gate
    require_ai_approval = Column(Boolean, default=True, nullable=False)
    follow_email_notifications_enabled = Column(
        Boolean,
        default=False,
        nullable=False,
    )

    # ── Multi-Layer Risk Protection ─────────────────────
    monthly_loss_limit = Column(Float, nullable=True)            # USDC rolling-30-day loss cap
    max_drawdown_pct = Column(Float, default=25.0, nullable=False)  # % drawdown from peak
    total_loss_halt_pct = Column(Float, default=40.0, nullable=False)  # % total loss → permanent halt
    peak_capital = Column(Float, nullable=True)                  # High-water mark in USDC
    initial_capital = Column(Float, nullable=True)               # Baseline capital for total-loss calc
    trading_halted = Column(Boolean, default=False, nullable=False)  # Permanent halt flag
    halt_reason = Column(String(200), nullable=True)             # Why trading was halted
    cooldown_until = Column(DateTime(timezone=True), nullable=True)  # Timed pause expiry

    # ── Dynamic Streak-Based Sizing ───────────────────
    dynamic_sizing_enabled = Column(Boolean, default=False, nullable=False)
    consecutive_wins = Column(Integer, default=0, nullable=False)
    consecutive_losses = Column(Integer, default=0, nullable=False)

    # ── Simulation / Dry-Run Mode ─────────────────────
    simulation_mode = Column(Boolean, default=False, nullable=False)

    # ── Inverse Position Bot Settings ───────────────────────
    inverse_bot_enabled = Column(Boolean, default=False, nullable=False)
    inverse_bot_default_size_mode = Column(
        String(20),
        default=InverseBotSizeMode.FULL_NOTIONAL.value,
        nullable=False,
    )
    inverse_bot_fixed_amount = Column(Float, default=50.0, nullable=False)
    inverse_bot_confidence_threshold = Column(Integer, default=75, nullable=False)
    inverse_bot_cooldown_minutes = Column(Integer, default=30, nullable=False)
    inverse_bot_max_reversals_per_day = Column(Integer, default=3, nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    
    def __repr__(self):
        return f"<UserSettings(user_id={self.user_id}, ai_backend={self.ai_backend})>"
