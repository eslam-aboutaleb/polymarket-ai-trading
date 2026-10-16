"""Per-account settings — a ``user_settings``-shaped row per account (plan 06 Phase A).

``user_settings`` stays 1:1 with ``User`` and untouched: it is the
personal account's settings and remains the row the existing routes
read. Every *other* account gets its own row here, with the same column
set, so a sub-account's risk limits, simulation mode and paper balance
are genuinely independent of the owner's.

The copy is deliberate rather than a join: the personal account must keep
working unchanged (zero migration risk) while a sub-account can diverge
without a migration that rewrites ``user_settings`` semantics. The
CheckConstraints are re-declared here so a backfill or a seed script
cannot write an out-of-range value on either table.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)

from app.models.base import Base
from app.models.users.user_settings import (
    AIBackendType,
    InverseBotSizeMode,
    RiskMode,
)
from app.utils.time import utc_now


class AccountSettings(Base):
    """Settings row for one non-personal account."""

    __tablename__ = "account_settings"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True, nullable=False, index=True)

    ai_backend = Column(String(20), default=AIBackendType.LLM_CHAIN.value, nullable=False)
    preferred_llm_provider = Column(String(30), nullable=True, default=None)
    preferred_llm_model = Column(String(100), nullable=True, default=None)

    # ── Copy-Trading Settings ──────────────────────────────────
    copy_trading_enabled = Column(Boolean, default=False, nullable=False)
    risk_mode = Column(
        String(30),
        default=RiskMode.MAX_POSITION_DAILY_LOSS.value,
        nullable=False,
    )
    max_position_size = Column(Float, default=100.0)
    daily_loss_limit = Column(Float, default=500.0)
    mirror_percentage = Column(Float, default=10.0)
    fixed_trade_amount = Column(Float, default=50.0)
    kelly_fraction = Column(Float, default=0.25, nullable=False)
    require_ai_approval = Column(Boolean, default=True, nullable=False)
    follow_email_notifications_enabled = Column(Boolean, default=False, nullable=False)
    daily_digest_enabled = Column(Boolean, server_default="1", default=True, nullable=False)

    # ── Multi-Layer Risk Protection ─────────────────────
    monthly_loss_limit = Column(Float, nullable=True)
    max_drawdown_pct = Column(Float, default=25.0, nullable=False)
    total_loss_halt_pct = Column(Float, default=40.0, nullable=False)
    peak_capital = Column(Float, nullable=True)
    initial_capital = Column(Float, nullable=True)
    trading_halted = Column(Boolean, default=False, nullable=False)
    halt_reason = Column(String(200), nullable=True)
    cooldown_until = Column(DateTime(timezone=True), nullable=True)

    # ── Dynamic Streak-Based Sizing ───────────────────
    dynamic_sizing_enabled = Column(Boolean, default=False, nullable=False)
    consecutive_wins = Column(Integer, default=0, nullable=False)
    consecutive_losses = Column(Integer, default=0, nullable=False)

    # ── Simulation / Dry-Run Mode ─────────────────────
    simulation_mode = Column(Boolean, default=False, nullable=False)
    paper_balance = Column(Float, default=1000.0, nullable=False)

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

    # Same bounds as user_settings, renamed so both tables can coexist.
    __table_args__ = (
        CheckConstraint(
            "(mirror_percentage IS NULL OR (mirror_percentage >= 0 AND mirror_percentage <= 100))",
            name="ck_account_settings_mirror_percentage_range",
        ),
        CheckConstraint(
            "(inverse_bot_confidence_threshold >= 0 AND inverse_bot_confidence_threshold <= 100)",
            name="ck_account_settings_inverse_confidence_range",
        ),
        CheckConstraint(
            "(max_position_size IS NULL OR max_position_size >= 0)",
            name="ck_account_settings_max_position_size_non_negative",
        ),
        CheckConstraint(
            "(daily_loss_limit IS NULL OR daily_loss_limit >= 0)",
            name="ck_account_settings_daily_loss_limit_non_negative",
        ),
        CheckConstraint(
            "(fixed_trade_amount IS NULL OR fixed_trade_amount >= 0)",
            name="ck_account_settings_fixed_trade_amount_non_negative",
        ),
        CheckConstraint(
            "(inverse_bot_fixed_amount >= 0)",
            name="ck_account_settings_inverse_fixed_amount_non_negative",
        ),
        CheckConstraint(
            "(inverse_bot_cooldown_minutes >= 0)",
            name="ck_account_settings_inverse_cooldown_non_negative",
        ),
        CheckConstraint(
            "(inverse_bot_max_reversals_per_day >= 0)",
            name="ck_account_settings_inverse_max_reversals_non_negative",
        ),
        CheckConstraint(
            "(kelly_fraction >= 0 AND kelly_fraction <= 1)",
            name="ck_account_settings_kelly_fraction_range",
        ),
    )

    def __repr__(self):
        return f"<AccountSettings(account_id={self.account_id})>"
