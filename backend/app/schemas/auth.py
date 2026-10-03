"""Authentication schemas"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

WALLET_ADDRESS_PATTERN = r"^0x[a-fA-F0-9]{40}$"


class LoginRequest(BaseModel):
    """Login request with wallet address"""

    wallet_address: str = Field(
        ...,
        pattern=WALLET_ADDRESS_PATTERN,
        description="Ethereum wallet address (0x...)",
    )


class ChallengeResponse(BaseModel):
    """Challenge response from /login endpoint"""

    challenge_id: str = Field(
        ...,
        description="Opaque ID of this challenge; pass it back to /verify",
    )
    challenge: str = Field(..., description="Challenge message to sign")
    timestamp: str = Field(..., description="Timestamp used in challenge")
    nonce: str = Field(..., description="Unique nonce for this challenge")
    message: str = Field(..., description="Human-readable instruction")


class SignatureVerification(BaseModel):
    """Verify wallet signature - include wallet address for lookup"""

    wallet_address: str = Field(
        ...,
        pattern=WALLET_ADDRESS_PATTERN,
        description="Ethereum wallet address (0x...)",
    )
    signature: str = Field(..., description="Signed message from wallet")
    challenge_id: str | None = Field(
        None,
        description=(
            "challenge_id returned by /login (preferred; keys the challenge "
            "lookup so a pending challenge cannot be overwritten)"
        ),
    )
    keep_logged_in: bool = Field(
        default=False, description="If True, refresh token expires in 90 days; else 7 days"
    )


class TokenResponse(BaseModel):
    """Token response on successful authentication"""

    access_token: str | None = Field(
        None,
        description=(
            "JWT access token (15 min expiry). Login endpoints omit this and "
            "set an httpOnly cookie instead; only /refresh returns it."
        ),
    )
    refresh_token: str | None = Field(
        None,
        description=(
            "Refresh token (7 or 90 day expiry). Login endpoints omit this and "
            "set an httpOnly cookie instead; only /refresh returns it."
        ),
    )
    token_type: str = Field(default="bearer")
    expires_in: int = Field(..., description="Access token expiry in seconds")
    wallet_address: str | None = Field(None, description="Wallet address of the authenticated user")


class TokenRefresh(BaseModel):
    """Refresh token request"""

    refresh_token: str | None = Field(
        None,
        description=(
            "Refresh token to exchange for new access token (optional when cookie auth is enabled)"
        ),
    )


class UserResponse(BaseModel):
    """Current user info"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    wallet_address: str
    created_at: datetime
    last_login: datetime | None
    is_admin: bool = False


class LogoutRequest(BaseModel):
    """Logout request"""

    refresh_token: str | None = Field(
        None, description="Optional refresh token to explicitly revoke"
    )


class PrivateKeyLoginRequest(BaseModel):
    """Login request using private key directly"""

    private_key: str = Field(..., description="Ethereum private key (0x... or hex string)")
    keep_logged_in: bool = Field(
        default=False, description="If True, refresh token expires in 90 days; else 7 days"
    )
