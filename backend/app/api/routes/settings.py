"""User Settings API routes"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.config import AIBackend
from app.models.user import User
from app.models.user_settings import (
    UserSettings,
    AIBackendType,
    RiskMode,
    InverseBotSizeMode,
)
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(prefix="/api/settings", tags=["settings"])


# Request/Response schemas
class UserSettingsResponse(BaseModel):
    """User settings response"""
    ai_backend: str = Field(..., description="Selected AI backend")
    copy_trading_enabled: bool = False
    risk_mode: str = "max_position_daily_loss"
    max_position_size: float = 100.0
    daily_loss_limit: float = 500.0
    mirror_percentage: float = 10.0
    fixed_trade_amount: float = 50.0
    require_ai_approval: bool = True
    follow_email_notifications_enabled: bool = False
    inverse_bot_enabled: bool = False
    inverse_bot_default_size_mode: str = InverseBotSizeMode.FULL_NOTIONAL.value
    inverse_bot_fixed_amount: float = 50.0
    inverse_bot_confidence_threshold: int = 75
    inverse_bot_cooldown_minutes: int = 30
    inverse_bot_max_reversals_per_day: int = 3
    updated_at: Optional[str] = None
    
    class Config:
        from_attributes = True


class UpdateSettingsRequest(BaseModel):
    """Request to update user settings"""
    ai_backend: Optional[str] = Field(
        None, 
        description="AI backend: llm_chain or cli_agent"
    )


class CopyTradingSettingsRequest(BaseModel):
    """Request to update copy-trading settings."""
    copy_trading_enabled: Optional[bool] = None
    risk_mode: Optional[str] = Field(None, pattern="^(max_position_daily_loss|percentage_mirror|fixed_amount)$")
    max_position_size: Optional[float] = Field(None, gt=0)
    daily_loss_limit: Optional[float] = Field(None, gt=0)
    mirror_percentage: Optional[float] = Field(None, gt=0, le=100)
    fixed_trade_amount: Optional[float] = Field(None, gt=0)
    require_ai_approval: Optional[bool] = None
    follow_email_notifications_enabled: Optional[bool] = None
    inverse_bot_enabled: Optional[bool] = None
    inverse_bot_default_size_mode: Optional[str] = Field(
        None, pattern="^(full_notional|fixed_amount)$"
    )
    inverse_bot_fixed_amount: Optional[float] = Field(None, gt=0)
    inverse_bot_confidence_threshold: Optional[int] = Field(None, ge=0, le=100)
    inverse_bot_cooldown_minutes: Optional[int] = Field(None, ge=0, le=1440)
    inverse_bot_max_reversals_per_day: Optional[int] = Field(None, ge=0, le=100)


class AIBackendStatusResponse(BaseModel):
    """Status of AI backends"""
    llm_chain: dict
    cli_agent: dict


# ── Profile Schemas ────────────────────────────────────────────
class UserProfileResponse(BaseModel):
    """User profile response"""
    wallet_address: str
    display_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    profile_picture_url: Optional[str] = None
    two_fa_enabled: bool = False

    class Config:
        from_attributes = True


class UpdateProfileRequest(BaseModel):
    """Request to update user profile"""
    display_name: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=30)
    profile_picture_url: Optional[str] = None
    two_fa_enabled: Optional[bool] = None


@router.get("", response_model=UserSettingsResponse)
async def get_user_settings(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """Get current user settings including copy-trading config."""
    user_id = current_user.get("user_id")
    
    settings = db.query(UserSettings).filter(
        UserSettings.user_id == user_id
    ).first()
    
    if not settings:
        return UserSettingsResponse(
            ai_backend=AIBackendType.LLM_CHAIN.value,
            updated_at=None
        )
    
    return UserSettingsResponse(
        ai_backend=settings.ai_backend,
        copy_trading_enabled=settings.copy_trading_enabled or False,
        risk_mode=settings.risk_mode or RiskMode.MAX_POSITION_DAILY_LOSS.value,
        max_position_size=settings.max_position_size or 100.0,
        daily_loss_limit=settings.daily_loss_limit or 500.0,
        mirror_percentage=settings.mirror_percentage or 10.0,
        fixed_trade_amount=settings.fixed_trade_amount or 50.0,
        require_ai_approval=settings.require_ai_approval if settings.require_ai_approval is not None else True,
        follow_email_notifications_enabled=(
            settings.follow_email_notifications_enabled
            if settings.follow_email_notifications_enabled is not None
            else False
        ),
        inverse_bot_enabled=settings.inverse_bot_enabled or False,
        inverse_bot_default_size_mode=(
            settings.inverse_bot_default_size_mode
            or InverseBotSizeMode.FULL_NOTIONAL.value
        ),
        inverse_bot_fixed_amount=settings.inverse_bot_fixed_amount or 50.0,
        inverse_bot_confidence_threshold=settings.inverse_bot_confidence_threshold or 75,
        inverse_bot_cooldown_minutes=settings.inverse_bot_cooldown_minutes or 30,
        inverse_bot_max_reversals_per_day=settings.inverse_bot_max_reversals_per_day or 3,
        updated_at=settings.updated_at.isoformat() if settings.updated_at else None,
    )


@router.put("", response_model=UserSettingsResponse)
async def update_user_settings(
    request: UpdateSettingsRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """Update user settings (AI backend)."""
    user_id = current_user.get("user_id")
    
    if request.ai_backend:
        valid_backends = [b.value for b in AIBackendType]
        if request.ai_backend not in valid_backends:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid ai_backend. Must be one of: {valid_backends}"
            )
    
    settings = db.query(UserSettings).filter(
        UserSettings.user_id == user_id
    ).first()
    
    if not settings:
        settings = UserSettings(
            user_id=user_id,
            ai_backend=request.ai_backend or AIBackendType.LLM_CHAIN.value
        )
        db.add(settings)
    else:
        if request.ai_backend:
            settings.ai_backend = request.ai_backend
        settings.updated_at = utc_now()
    
    db.commit()
    db.refresh(settings)
    
    return UserSettingsResponse(
        ai_backend=settings.ai_backend,
        copy_trading_enabled=settings.copy_trading_enabled or False,
        risk_mode=settings.risk_mode or RiskMode.MAX_POSITION_DAILY_LOSS.value,
        max_position_size=settings.max_position_size or 100.0,
        daily_loss_limit=settings.daily_loss_limit or 500.0,
        mirror_percentage=settings.mirror_percentage or 10.0,
        fixed_trade_amount=settings.fixed_trade_amount or 50.0,
        require_ai_approval=settings.require_ai_approval if settings.require_ai_approval is not None else True,
        follow_email_notifications_enabled=(
            settings.follow_email_notifications_enabled
            if settings.follow_email_notifications_enabled is not None
            else False
        ),
        inverse_bot_enabled=settings.inverse_bot_enabled or False,
        inverse_bot_default_size_mode=(
            settings.inverse_bot_default_size_mode
            or InverseBotSizeMode.FULL_NOTIONAL.value
        ),
        inverse_bot_fixed_amount=settings.inverse_bot_fixed_amount or 50.0,
        inverse_bot_confidence_threshold=settings.inverse_bot_confidence_threshold or 75,
        inverse_bot_cooldown_minutes=settings.inverse_bot_cooldown_minutes or 30,
        inverse_bot_max_reversals_per_day=settings.inverse_bot_max_reversals_per_day or 3,
        updated_at=settings.updated_at.isoformat() if settings.updated_at else None,
    )


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

    settings = db.query(UserSettings).filter(
        UserSettings.user_id == user_id
    ).first()

    if not settings:
        settings = UserSettings(
            user_id=user_id,
            ai_backend=AIBackendType.LLM_CHAIN.value,
        )
        db.add(settings)
        db.flush()

    if request.copy_trading_enabled is not None:
        settings.copy_trading_enabled = request.copy_trading_enabled
    if request.risk_mode is not None:
        settings.risk_mode = request.risk_mode
    if request.max_position_size is not None:
        settings.max_position_size = request.max_position_size
    if request.daily_loss_limit is not None:
        settings.daily_loss_limit = request.daily_loss_limit
    if request.mirror_percentage is not None:
        settings.mirror_percentage = request.mirror_percentage
    if request.fixed_trade_amount is not None:
        settings.fixed_trade_amount = request.fixed_trade_amount
    if request.require_ai_approval is not None:
        settings.require_ai_approval = request.require_ai_approval
    if request.follow_email_notifications_enabled is not None:
        settings.follow_email_notifications_enabled = (
            request.follow_email_notifications_enabled
        )
    if request.inverse_bot_enabled is not None:
        settings.inverse_bot_enabled = request.inverse_bot_enabled
    if request.inverse_bot_default_size_mode is not None:
        settings.inverse_bot_default_size_mode = request.inverse_bot_default_size_mode
    if request.inverse_bot_fixed_amount is not None:
        settings.inverse_bot_fixed_amount = request.inverse_bot_fixed_amount
    if request.inverse_bot_confidence_threshold is not None:
        settings.inverse_bot_confidence_threshold = request.inverse_bot_confidence_threshold
    if request.inverse_bot_cooldown_minutes is not None:
        settings.inverse_bot_cooldown_minutes = request.inverse_bot_cooldown_minutes
    if request.inverse_bot_max_reversals_per_day is not None:
        settings.inverse_bot_max_reversals_per_day = request.inverse_bot_max_reversals_per_day

    settings.updated_at = utc_now()
    db.commit()
    db.refresh(settings)

    return UserSettingsResponse(
        ai_backend=settings.ai_backend,
        copy_trading_enabled=settings.copy_trading_enabled or False,
        risk_mode=settings.risk_mode or RiskMode.MAX_POSITION_DAILY_LOSS.value,
        max_position_size=settings.max_position_size or 100.0,
        daily_loss_limit=settings.daily_loss_limit or 500.0,
        mirror_percentage=settings.mirror_percentage or 10.0,
        fixed_trade_amount=settings.fixed_trade_amount or 50.0,
        require_ai_approval=settings.require_ai_approval if settings.require_ai_approval is not None else True,
        follow_email_notifications_enabled=(
            settings.follow_email_notifications_enabled
            if settings.follow_email_notifications_enabled is not None
            else False
        ),
        inverse_bot_enabled=settings.inverse_bot_enabled or False,
        inverse_bot_default_size_mode=(
            settings.inverse_bot_default_size_mode
            or InverseBotSizeMode.FULL_NOTIONAL.value
        ),
        inverse_bot_fixed_amount=settings.inverse_bot_fixed_amount or 50.0,
        inverse_bot_confidence_threshold=settings.inverse_bot_confidence_threshold or 75,
        inverse_bot_cooldown_minutes=settings.inverse_bot_cooldown_minutes or 30,
        inverse_bot_max_reversals_per_day=settings.inverse_bot_max_reversals_per_day or 3,
        updated_at=settings.updated_at.isoformat() if settings.updated_at else None,
    )


@router.get("/backends/status", response_model=AIBackendStatusResponse)
async def get_backends_status(
    current_user: dict = Depends(get_current_user_from_token)
):
    """
    Get health status of AI backends.
    Useful for frontend to show service availability.
    """
    from app.grpc_clients.analysis_client import AnalysisClient
    from app.config import AIBackend as ConfigAIBackend
    
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
            "status": "online" if llm_healthy else "offline"
        },
        cli_agent={
            "name": "CLI Agent (GitHub Copilot)",
            "description": "GitHub Copilot CLI with MCP tools",
            "healthy": cli_healthy,
            "status": "online" if cli_healthy else "offline"
        }
    )


# ── Profile Endpoints ─────────────────────────────────────────
@router.get("/profile", response_model=UserProfileResponse)
async def get_user_profile(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get current user profile (name, avatar, contact info)."""
    user_id = current_user.get("user_id")
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserProfileResponse(
        wallet_address=user.wallet_address,
        display_name=user.display_name or user.wallet_address,
        email=user.email,
        phone=user.phone,
        profile_picture_url=user.profile_picture_url,
        two_fa_enabled=user.two_fa_enabled or False,
    )


@router.put("/profile", response_model=UserProfileResponse)
async def update_user_profile(
    request: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Update user profile fields."""
    user_id = current_user.get("user_id")
    user = db.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if request.display_name is not None:
        user.display_name = request.display_name or None  # empty string → null
    if request.email is not None:
        user.email = request.email or None
    if request.phone is not None:
        user.phone = request.phone or None
    if request.profile_picture_url is not None:
        user.profile_picture_url = request.profile_picture_url or None
    if request.two_fa_enabled is not None:
        # Only allow enabling 2FA if email or phone is set
        if request.two_fa_enabled and not (user.email or user.phone or request.email or request.phone):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email or phone is required to enable 2FA",
            )
        user.two_fa_enabled = request.two_fa_enabled

    db.commit()
    db.refresh(user)

    return UserProfileResponse(
        wallet_address=user.wallet_address,
        display_name=user.display_name or user.wallet_address,
        email=user.email,
        phone=user.phone,
        profile_picture_url=user.profile_picture_url,
        two_fa_enabled=user.two_fa_enabled or False,
    )
