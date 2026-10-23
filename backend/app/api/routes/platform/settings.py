"""User Settings API routes"""

from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_strict_account
from app.api.routes.auth.auth import get_current_user_from_token
from app.models import Account
from app.models.users.user import validate_profile_picture_url
from app.models.users.user_settings import (
    AIBackendType,
    InverseBotSizeMode,
    RiskMode,
    UserSettings,
)
from app.services.intelligence.llm_gateway import LLMProviderType as GatewayProviderType
from app.services.intelligence.llm_gateway import get_llm_gateway
from app.services.users import settings_service
from app.utils.database import get_db

router = APIRouter(prefix="/api/settings", tags=["settings"])


# Request/Response schemas
class UserSettingsResponse(BaseModel):
    """User settings response"""

    ai_backend: str = Field(..., description="Selected AI backend (legacy)")
    preferred_llm_provider: str | None = Field(None, description="Preferred LLM provider")
    preferred_llm_model: str | None = Field(None, description="Preferred model for the provider")
    copy_trading_enabled: bool = False
    risk_mode: str = "max_position_daily_loss"
    max_position_size: float = 100.0
    daily_loss_limit: float = 500.0
    mirror_percentage: float = 10.0
    fixed_trade_amount: float = 50.0
    kelly_fraction: float = Field(
        0.25,
        ge=0,
        le=1,
        description="Fractional-Kelly multiplier for the kelly sizing mode",
    )
    require_ai_approval: bool = True
    follow_email_notifications_enabled: bool = False
    daily_digest_enabled: bool = True
    # Multi-layer risk protection
    monthly_loss_limit: float | None = None
    max_drawdown_pct: float = 25.0
    total_loss_halt_pct: float = 40.0
    peak_capital: float | None = None
    initial_capital: float | None = None
    trading_halted: bool = False
    halt_reason: str | None = None
    cooldown_until: str | None = None
    # Dynamic sizing
    dynamic_sizing_enabled: bool = False
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    # Simulation mode
    simulation_mode: bool = False
    paper_balance: float = Field(
        1000.0,
        ge=0,
        description="Paper account balance (USDC) — paper PnL is tracked against this",
    )
    # Inverse bot
    inverse_bot_enabled: bool = False
    inverse_bot_default_size_mode: str = InverseBotSizeMode.FULL_NOTIONAL.value
    inverse_bot_fixed_amount: float = 50.0
    inverse_bot_confidence_threshold: int = 75
    inverse_bot_cooldown_minutes: int = 30
    inverse_bot_max_reversals_per_day: int = 3
    updated_at: str | None = None

    model_config = ConfigDict(from_attributes=True)


class LLMProviderInfo(BaseModel):
    """Information about an LLM provider"""

    id: str = Field(..., description="Provider identifier")
    name: str = Field(..., description="Display name")
    backend: str = Field(..., description="Backend service (llm_chain or cli_agent)")
    models: list[str] = Field(default_factory=list, description="Available models")
    description: str = Field("", description="Provider description")
    requires_api_key: bool = Field(True, description="Whether API key is required")


class LLMProvidersResponse(BaseModel):
    """Response listing available LLM providers"""

    providers: list[LLMProviderInfo]
    default_provider: str = Field(..., description="System default provider")


class LLMCurrentSettingsResponse(BaseModel):
    """Current user's LLM settings"""

    provider: str | None = Field(None, description="Currently configured provider")
    model: str | None = Field(None, description="Currently configured model")
    effective_provider: str = Field(
        ..., description="Provider that will be used (considering defaults)"
    )
    available_models: list[str] = Field(default_factory=list)


class UpdateLLMSettingsRequest(BaseModel):
    """Request to update LLM provider settings"""

    provider: str | None = Field(
        None, description="LLM provider: openai, anthropic, google, groq, ollama, github_models"
    )
    model: str | None = Field(None, description="Model name (leave empty for provider default)")


class UpdateSettingsRequest(BaseModel):
    """Request to update user settings"""

    ai_backend: str | None = Field(None, description="AI backend: llm_chain or cli_agent")
    kelly_fraction: float | None = Field(
        None,
        ge=0,
        le=1,
        description="Fractional-Kelly multiplier (0-1) for the kelly sizing mode",
    )


class CopyTradingSettingsRequest(BaseModel):
    """Request to update copy-trading settings."""

    copy_trading_enabled: bool | None = None
    risk_mode: str | None = Field(
        None, pattern="^(max_position_daily_loss|percentage_mirror|fixed_amount)$"
    )
    max_position_size: float | None = Field(None, gt=0)
    daily_loss_limit: float | None = Field(None, gt=0)
    mirror_percentage: float | None = Field(None, gt=0, le=100)
    fixed_trade_amount: float | None = Field(None, gt=0)
    kelly_fraction: float | None = Field(
        None,
        ge=0,
        le=1,
        description="Fractional-Kelly multiplier (0-1) for the kelly sizing mode",
    )
    require_ai_approval: bool | None = None
    follow_email_notifications_enabled: bool | None = None
    daily_digest_enabled: bool | None = None
    # Multi-layer risk protection
    monthly_loss_limit: float | None = Field(None, ge=0)
    max_drawdown_pct: float | None = Field(None, gt=0, le=100)
    total_loss_halt_pct: float | None = Field(None, gt=0, le=100)
    initial_capital: float | None = Field(None, gt=0)
    # Dynamic sizing
    dynamic_sizing_enabled: bool | None = None
    # Simulation mode
    simulation_mode: bool | None = None
    paper_balance: float | None = Field(
        None,
        ge=0,
        description="Paper account balance (USDC) — paper PnL is tracked against this",
    )
    # Inverse bot
    inverse_bot_enabled: bool | None = None
    inverse_bot_default_size_mode: str | None = Field(
        None, pattern="^(full_notional|fixed_amount)$"
    )
    inverse_bot_fixed_amount: float | None = Field(None, gt=0)
    inverse_bot_confidence_threshold: int | None = Field(None, ge=0, le=100)
    inverse_bot_cooldown_minutes: int | None = Field(None, ge=0, le=1440)
    inverse_bot_max_reversals_per_day: int | None = Field(None, ge=0, le=100)


class AIBackendStatusResponse(BaseModel):
    """Status of AI backends"""

    llm_chain: dict
    cli_agent: dict


# ── Profile Schemas ────────────────────────────────────────────
class UserProfileResponse(BaseModel):
    """User profile response"""

    wallet_address: str
    display_name: str
    email: str | None = None
    phone: str | None = None
    profile_picture_url: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UpdateProfileRequest(BaseModel):
    """Request to update user profile"""

    display_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=30)
    profile_picture_url: str | None = None

    @field_validator("profile_picture_url")
    @classmethod
    def _validate_profile_picture_url(cls, value: str | None) -> str | None:
        return validate_profile_picture_url(value)


def _build_settings_response(settings: UserSettings) -> UserSettingsResponse:
    """Build a UserSettingsResponse from a UserSettings ORM instance."""
    return UserSettingsResponse(
        ai_backend=settings.ai_backend,
        preferred_llm_provider=settings.preferred_llm_provider,
        preferred_llm_model=settings.preferred_llm_model,
        copy_trading_enabled=settings.copy_trading_enabled or False,
        risk_mode=settings.risk_mode or RiskMode.MAX_POSITION_DAILY_LOSS.value,
        max_position_size=settings.max_position_size or 100.0,
        daily_loss_limit=settings.daily_loss_limit or 500.0,
        mirror_percentage=settings.mirror_percentage or 10.0,
        fixed_trade_amount=settings.fixed_trade_amount or 50.0,
        kelly_fraction=(settings.kelly_fraction if settings.kelly_fraction is not None else 0.25),
        require_ai_approval=settings.require_ai_approval
        if settings.require_ai_approval is not None
        else True,
        follow_email_notifications_enabled=(
            settings.follow_email_notifications_enabled
            if settings.follow_email_notifications_enabled is not None
            else False
        ),
        daily_digest_enabled=(
            settings.daily_digest_enabled if settings.daily_digest_enabled is not None else True
        ),
        monthly_loss_limit=settings.monthly_loss_limit,
        max_drawdown_pct=settings.max_drawdown_pct
        if settings.max_drawdown_pct is not None
        else 25.0,
        total_loss_halt_pct=settings.total_loss_halt_pct
        if settings.total_loss_halt_pct is not None
        else 40.0,
        peak_capital=settings.peak_capital,
        initial_capital=settings.initial_capital,
        trading_halted=settings.trading_halted or False,
        halt_reason=settings.halt_reason,
        cooldown_until=settings.cooldown_until.isoformat() if settings.cooldown_until else None,
        dynamic_sizing_enabled=settings.dynamic_sizing_enabled or False,
        consecutive_wins=settings.consecutive_wins or 0,
        consecutive_losses=settings.consecutive_losses or 0,
        simulation_mode=settings.simulation_mode or False,
        paper_balance=(settings.paper_balance if settings.paper_balance is not None else 1000.0),
        inverse_bot_enabled=settings.inverse_bot_enabled or False,
        inverse_bot_default_size_mode=(
            settings.inverse_bot_default_size_mode or InverseBotSizeMode.FULL_NOTIONAL.value
        ),
        inverse_bot_fixed_amount=settings.inverse_bot_fixed_amount or 50.0,
        inverse_bot_confidence_threshold=settings.inverse_bot_confidence_threshold or 75,
        inverse_bot_cooldown_minutes=settings.inverse_bot_cooldown_minutes or 30,
        inverse_bot_max_reversals_per_day=settings.inverse_bot_max_reversals_per_day or 3,
        updated_at=settings.updated_at.isoformat() if settings.updated_at else None,
    )


@router.get("", response_model=UserSettingsResponse)
async def get_user_settings(
    current_user: dict = Depends(get_current_user_from_token), db: Session = Depends(get_db)
):
    """Get current user settings including copy-trading config."""
    user_id = current_user.get("user_id")

    settings = settings_service.get_settings(db, user_id)

    if not settings:
        return UserSettingsResponse(
            ai_backend=AIBackendType.LLM_CHAIN.value,
            preferred_llm_provider=None,
            preferred_llm_model=None,
            updated_at=None,
        )

    return _build_settings_response(settings)


@router.put("", response_model=UserSettingsResponse)
async def update_user_settings(
    request: UpdateSettingsRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Update user settings (AI backend)."""
    user_id = current_user.get("user_id")

    if request.ai_backend:
        valid_backends = [b.value for b in AIBackendType]
        if request.ai_backend not in valid_backends:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid ai_backend. Must be one of: {valid_backends}",
            )

    settings = settings_service.update_ai_settings(
        db,
        user_id,
        ai_backend=request.ai_backend,
        kelly_fraction=request.kelly_fraction,
    )

    return _build_settings_response(settings)


@router.get("/copy-trading", response_model=UserSettingsResponse)
async def get_copy_trading_settings(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get copy-trading specific settings."""
    return await get_user_settings(current_user, db)


@router.put("/copy-trading", response_model=UserSettingsResponse)
async def update_copy_trading_settings(
    request: CopyTradingSettingsRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Update copy-trading configuration."""
    user_id = current_user.get("user_id")

    settings = settings_service.update_copy_trading_settings(
        db,
        user_id,
        changes=request.model_dump(exclude_none=True),
    )

    return _build_settings_response(settings)


class PaperSummaryResponse(BaseModel):
    """Paper-trading summary, tracked separately from real equity."""

    simulation_mode: bool = False
    paper_balance: float = 1000.0
    paper_pnl: float | None = None
    paper_nav: float | None = None
    quality: str = "legacy_unverified"
    model_version: str = "legacy_unverified"
    legacy_simulated_trades: int = 0
    simulated_trades: int = 0


@router.get("/paper-summary", response_model=PaperSummaryResponse)
async def get_paper_summary(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
    account: Account = Depends(get_strict_account),
):
    """Paper-trading summary: balance and realized paper PnL.

    Paper PnL is computed from the user's simulated trades and is
    tracked against ``paper_balance``, separate from real equity.
    """
    from app.services.analytics.simulation import get_paper_summary as _paper_summary

    user_id = current_user.get("user_id")
    return PaperSummaryResponse(**_paper_summary(db, user_id, account.id))


@router.get("/backends/status", response_model=AIBackendStatusResponse)
async def get_backends_status(current_user: dict = Depends(get_current_user_from_token)):
    """
    Get health status of AI backends.
    Useful for frontend to show service availability.
    """
    from app.config import AIBackend as ConfigAIBackend
    from app.grpc_clients.analysis_client import AnalysisClient

    # Check LLM Chain service
    llm_client = AnalysisClient(backend=ConfigAIBackend.LLM_CHAIN)
    llm_healthy = await llm_client.health_check()
    await llm_client.close()

    # Check CLI Agent service
    cli_client = AnalysisClient(backend=ConfigAIBackend.CLI_AGENT)
    cli_healthy = await cli_client.health_check()
    await cli_client.close()

    return AIBackendStatusResponse(
        llm_chain={
            "name": "LLM Chain (OpenAI)",
            "description": "LangChain with OpenAI GPT-4 and web search",
            "healthy": llm_healthy,
            "status": "online" if llm_healthy else "offline",
        },
        cli_agent={
            "name": "CLI Agent (GitHub Copilot)",
            "description": "GitHub Copilot CLI with MCP tools",
            "healthy": cli_healthy,
            "status": "online" if cli_healthy else "offline",
        },
    )


# ── Profile Endpoints ─────────────────────────────────────────
@router.get("/profile", response_model=UserProfileResponse)
async def get_user_profile(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get current user profile (name, avatar, contact info)."""
    user_id = current_user.get("user_id")
    user = settings_service.get_profile(db, user_id)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfileResponse(
        wallet_address=user.wallet_address,
        display_name=user.display_name or user.wallet_address,
        email=user.email,
        phone=user.phone,
        profile_picture_url=user.profile_picture_url,
    )


@router.put("/profile", response_model=UserProfileResponse)
async def update_user_profile(
    request: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Update user profile fields."""
    user_id = current_user.get("user_id")
    user = settings_service.update_profile(
        db,
        user_id,
        display_name=request.display_name,
        email=request.email,
        phone=request.phone,
        profile_picture_url=request.profile_picture_url,
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfileResponse(
        wallet_address=user.wallet_address,
        display_name=user.display_name or user.wallet_address,
        email=user.email,
        phone=user.phone,
        profile_picture_url=user.profile_picture_url,
    )


# ── LLM Provider Endpoints ────────────────────────────────────


@router.get("/llm/providers", response_model=LLMProvidersResponse)
async def get_llm_providers(
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    List all available LLM providers.

    Returns information about each provider including:
    - Available models
    - Whether API key is required
    - Backend service that handles the provider
    """
    gateway = get_llm_gateway()
    providers_list = gateway.list_providers()

    providers = [
        LLMProviderInfo(
            id=p["id"],
            name=p["name"],
            backend=p["backend"],
            models=p["models"],
            description=p.get("description", ""),
            requires_api_key=p.get("requires_api_key", True),
        )
        for p in providers_list
    ]

    return LLMProvidersResponse(
        providers=providers,
        default_provider=GatewayProviderType.OPENAI.value,
    )


@router.get("/llm/current", response_model=LLMCurrentSettingsResponse)
async def get_current_llm_settings(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Get the current user's LLM provider settings.

    Returns:
    - User's configured provider and model
    - The effective provider (what will actually be used)
    - Available models for the effective provider
    """
    user_id = current_user.get("user_id")
    get_llm_gateway()

    settings = settings_service.get_settings(db, user_id)

    user_provider = None
    user_model = None

    if settings:
        user_provider = settings.preferred_llm_provider
        user_model = settings.preferred_llm_model

    # Determine effective provider
    if user_provider:
        effective_provider = user_provider
    elif settings and settings.ai_backend == AIBackendType.CLI_AGENT.value:
        effective_provider = GatewayProviderType.GITHUB_MODELS.value
    else:
        effective_provider = GatewayProviderType.OPENAI.value

    # Get available models for effective provider
    try:
        provider_enum = GatewayProviderType(effective_provider)
        from app.services.intelligence.llm_gateway import MODEL_SUGGESTIONS

        available_models = MODEL_SUGGESTIONS.get(provider_enum, [])
    except ValueError:
        available_models = []

    return LLMCurrentSettingsResponse(
        provider=user_provider,
        model=user_model,
        effective_provider=effective_provider,
        available_models=available_models,
    )


@router.patch("/llm", response_model=LLMCurrentSettingsResponse)
async def update_llm_settings(
    request: UpdateLLMSettingsRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """
    Update user's preferred LLM provider and model.

    Set provider to null/empty to clear and use system default.
    Set model to null/empty to use the provider's default model.
    """
    user_id = current_user.get("user_id")

    # Validate provider if specified
    if request.provider:
        valid_providers = [p.value for p in GatewayProviderType]
        if request.provider.lower() not in valid_providers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid provider. Must be one of: {valid_providers}",
            )

    settings = settings_service.update_llm_preferences(
        db,
        user_id,
        provider=request.provider,
        model=request.model,
    )

    # Return current state
    user_provider = settings.preferred_llm_provider
    user_model = settings.preferred_llm_model

    if user_provider:
        effective_provider = user_provider
    elif settings.ai_backend == AIBackendType.CLI_AGENT.value:
        effective_provider = GatewayProviderType.GITHUB_MODELS.value
    else:
        effective_provider = GatewayProviderType.OPENAI.value

    try:
        provider_enum = GatewayProviderType(effective_provider)
        from app.services.intelligence.llm_gateway import MODEL_SUGGESTIONS

        available_models = MODEL_SUGGESTIONS.get(provider_enum, [])
    except ValueError:
        available_models = []

    return LLMCurrentSettingsResponse(
        provider=user_provider,
        model=user_model,
        effective_provider=effective_provider,
        available_models=available_models,
    )


# ── Admin LLM Settings Endpoints ────────────────────────────────


def require_admin(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Dependency that ensures caller is admin."""
    user_id = current_user.get("user_id")
    user = settings_service.get_profile(db, user_id) if user_id else None
    if not user or not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return {
        "user_id": user.id,
        "wallet_address": user.wallet_address,
        "is_admin": user.is_admin,
    }


# ── Admin Schemas ──


class AdminProviderStatus(BaseModel):
    """Provider status for admin view"""

    id: str
    name: str
    backend: str
    models: list[str]
    description: str
    requires_api_key: bool
    is_configured: bool = Field(..., description="Whether API key is set")
    is_healthy: bool = Field(..., description="Whether the provider service is reachable")


class AdminProvidersResponse(BaseModel):
    """Admin view of all providers with status"""

    providers: list[AdminProviderStatus]
    default_provider: str
    default_model: str


class AdminDefaultsRequest(BaseModel):
    """Request to update system defaults"""

    default_provider: str | None = None
    default_model: str | None = None


class AdminUserLLMSettings(BaseModel):
    """User's LLM settings for admin view"""

    user_id: int
    wallet_address: str
    display_name: str | None
    preferred_llm_provider: str | None
    preferred_llm_model: str | None
    ai_backend: str
    updated_at: str | None


class AdminUsersLLMResponse(BaseModel):
    """List of users with their LLM settings"""

    users: list[AdminUserLLMSettings]
    total: int


class AdminUpdateUserLLMRequest(BaseModel):
    """Request to update a user's LLM settings (admin override)"""

    preferred_llm_provider: str | None = None
    preferred_llm_model: str | None = None


# ── Admin Endpoints ──


@router.get("/admin/providers", response_model=AdminProvidersResponse)
async def get_admin_providers(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Get all LLM providers with their configuration and health status.
    Admin only.
    """
    import os

    from app.config import AIBackend as ConfigAIBackend
    from app.config import get_settings
    from app.grpc_clients.analysis_client import AnalysisClient

    get_settings()
    gateway = get_llm_gateway()
    providers_list = gateway.list_providers()

    # Check service health
    llm_chain_healthy = False
    cli_agent_healthy = False

    with suppress(Exception):
        client = AnalysisClient(backend=ConfigAIBackend.LLM_CHAIN)
        llm_chain_healthy = await client.health_check()

    with suppress(Exception):
        client = AnalysisClient(backend=ConfigAIBackend.CLI_AGENT)
        cli_agent_healthy = await client.health_check()

    # Check which providers have API keys configured
    configured_keys = {
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "google": bool(os.environ.get("GOOGLE_API_KEY")),
        "groq": bool(os.environ.get("GROQ_API_KEY")),
        "ollama": True,  # Ollama doesn't need an API key
        "github_models": bool(os.environ.get("GITHUB_TOKEN")),
    }

    providers = []
    for p in providers_list:
        backend = p["backend"]
        is_healthy = llm_chain_healthy if backend == "llm_chain" else cli_agent_healthy

        providers.append(
            AdminProviderStatus(
                id=p["id"],
                name=p["name"],
                backend=backend,
                models=p["models"],
                description=p.get("description", ""),
                requires_api_key=p.get("requires_api_key", True),
                is_configured=configured_keys.get(p["id"], False),
                is_healthy=is_healthy,
            )
        )

    return AdminProvidersResponse(
        providers=providers,
        default_provider=os.environ.get("LLM_PROVIDER", "openai"),
        default_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    )


@router.patch("/admin/defaults")
async def update_admin_defaults(
    request: AdminDefaultsRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Update system-wide default LLM provider and model.
    Admin only.
    """
    import os

    if request.default_provider:
        valid_providers = [p.value for p in GatewayProviderType]
        if request.default_provider.lower() not in valid_providers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid provider. Must be one of: {valid_providers}",
            )
        os.environ["LLM_PROVIDER"] = request.default_provider.lower()

    if request.default_model:
        os.environ["LLM_MODEL"] = request.default_model

    return {
        "success": True,
        "default_provider": os.environ.get("LLM_PROVIDER", "openai"),
        "default_model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        "note": (
            "Changes applied to current process. For persistence, update environment configuration."
        ),
    }


@router.get("/admin/users", response_model=AdminUsersLLMResponse)
async def get_admin_users_llm(
    skip: int = 0,
    limit: int = 50,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Get list of users with their LLM settings.
    Admin only.
    """
    total, users_with_settings = settings_service.admin_user_llm_rows(db, offset=skip, limit=limit)

    users = []
    for user, settings in users_with_settings:
        users.append(
            AdminUserLLMSettings(
                user_id=user.id,
                wallet_address=user.wallet_address,
                display_name=user.display_name,
                preferred_llm_provider=settings.preferred_llm_provider if settings else None,
                preferred_llm_model=settings.preferred_llm_model if settings else None,
                ai_backend=settings.ai_backend if settings else AIBackendType.LLM_CHAIN.value,
                updated_at=settings.updated_at.isoformat()
                if settings and settings.updated_at
                else None,
            )
        )

    return AdminUsersLLMResponse(users=users, total=total)


@router.patch("/admin/users/{user_id}/llm")
async def update_admin_user_llm(
    user_id: int,
    request: AdminUpdateUserLLMRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Override a user's LLM settings.
    Admin only.
    """
    user = settings_service.get_profile(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found"
        )

    if request.preferred_llm_provider:
        valid_providers = [p.value for p in GatewayProviderType]
        if request.preferred_llm_provider.lower() not in valid_providers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid provider. Must be one of: {valid_providers}",
            )

    settings = settings_service.update_llm_preferences(
        db,
        user_id,
        provider=request.preferred_llm_provider,
        model=request.preferred_llm_model,
    )

    return AdminUserLLMSettings(
        user_id=user.id,
        wallet_address=user.wallet_address,
        display_name=user.display_name,
        preferred_llm_provider=settings.preferred_llm_provider,
        preferred_llm_model=settings.preferred_llm_model,
        ai_backend=settings.ai_backend,
        updated_at=settings.updated_at.isoformat() if settings.updated_at else None,
    )
