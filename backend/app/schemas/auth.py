"""Authentication schemas"""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class LoginRequest(BaseModel):
    """Login request with wallet address"""
    wallet_address: str = Field(..., description="Ethereum wallet address (0x...)")


class ChallengeResponse(BaseModel):
    """Challenge response from /login endpoint"""
    challenge: str = Field(..., description="Challenge message to sign")
    timestamp: str = Field(..., description="Timestamp used in challenge")
    nonce: str = Field(..., description="Unique nonce for this challenge")
    message: str = Field(..., description="Human-readable instruction")


class SignatureVerification(BaseModel):
    """Verify wallet signature - include wallet address for lookup"""
    wallet_address: str = Field(..., description="Ethereum wallet address (0x...)")
    signature: str = Field(..., description="Signed message from wallet")
    keep_logged_in: bool = Field(
        default=False,
        description="If True, refresh token expires in 90 days; else 7 days"
    )


class TokenResponse(BaseModel):
    """Token response on successful authentication"""
    access_token: str = Field(..., description="JWT access token (15 min expiry)")
    refresh_token: str = Field(..., description="Refresh token (7 or 90 day expiry)")
    token_type: str = Field(default="bearer")
    expires_in: int = Field(..., description="Access token expiry in seconds")
    wallet_address: Optional[str] = Field(None, description="Wallet address of the authenticated user")


class TokenRefresh(BaseModel):
    """Refresh token request"""
    refresh_token: Optional[str] = Field(
        None,
        description="Refresh token to exchange for new access token (optional when cookie auth is enabled)",
    )


class UserResponse(BaseModel):
    """Current user info"""
    id: int
    wallet_address: str
    created_at: datetime
    last_login: Optional[datetime]
    is_admin: bool = False
    
    class Config:
        from_attributes = True


class LogoutRequest(BaseModel):
    """Logout request"""
    refresh_token: Optional[str] = Field(
        None,
        description="Optional refresh token to explicitly revoke"
    )


class PrivateKeyLoginRequest(BaseModel):
    """Login request using private key directly"""
    private_key: str = Field(..., description="Ethereum private key (0x... or hex string)")
    keep_logged_in: bool = Field(
        default=False,
        description="If True, refresh token expires in 90 days; else 7 days"
    )
