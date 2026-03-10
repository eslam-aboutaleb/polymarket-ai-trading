"""Authentication API routes"""
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request, Response
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Optional
from app.schemas.auth import (
    LoginRequest,
    SignatureVerification,
    TokenResponse,
    TokenRefresh,
    UserResponse,
    LogoutRequest,
    ChallengeResponse,
    PrivateKeyLoginRequest,
)
from app.models.user import User
from app.security.auth import (
    create_access_token,
    create_refresh_token,
    verify_token,
    verify_eth_signature,
    generate_challenge_message,
    sign_message_with_key,
)
from app.security.credential_store import (
    CredentialStoreError,
    delete_wallet_credentials,
    store_wallet_credentials,
)
from app.security.refresh_tokens import (
    create_refresh_token_record,
    find_active_refresh_token_record,
)
from app.utils.database import get_db
from app.utils.cache import challenge_cache
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

ACCESS_TOKEN_COOKIE_NAME = "pm_access_token"
REFRESH_TOKEN_COOKIE_NAME = "pm_refresh_token"


def _secure_cookie_enabled() -> bool:
    return not settings.is_local_environment


def _set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    refresh_max_age_seconds: int,
) -> None:
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE_NAME,
        value=access_token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=_secure_cookie_enabled(),
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=refresh_token,
        max_age=refresh_max_age_seconds,
        httponly=True,
        secure=_secure_cookie_enabled(),
        samesite="strict",
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        key=ACCESS_TOKEN_COOKIE_NAME,
        httponly=True,
        secure=_secure_cookie_enabled(),
        samesite="strict",
        path="/",
    )
    response.delete_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        httponly=True,
        secure=_secure_cookie_enabled(),
        samesite="strict",
        path="/",
    )


def _is_origin_allowed(origin: str) -> bool:
    allowed_origins = settings.cors_allowed_origins_list
    if origin in allowed_origins:
        return True
    parsed_origin = urlparse(origin)
    for allowed in allowed_origins:
        parsed_allowed = urlparse(allowed)
        if (
            parsed_origin.scheme == parsed_allowed.scheme
            and parsed_origin.netloc == parsed_allowed.netloc
        ):
            return True
    return False


def _validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    if not _is_origin_allowed(origin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin not allowed",
        )


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return None
    if parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


@router.post("/login", response_model=ChallengeResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    """
    Step 1: Initiate login with wallet address.
    Returns a challenge message for the user to sign.
    Stores challenge in cache for 5 minutes.
    """
    # Normalize wallet address
    wallet_address = request.wallet_address.lower()
    
    # Generate unique nonce and timestamp
    nonce = str(uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    challenge = generate_challenge_message(wallet_address, timestamp, nonce)
    
    # Store challenge in cache with 5 min expiry (keyed by wallet)
    challenge_data = {
        "challenge": challenge,
        "timestamp": timestamp,
        "nonce": nonce,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    challenge_cache.set(wallet_address, challenge_data, ttl_seconds=300)
    
    return ChallengeResponse(
        challenge=challenge,
        timestamp=timestamp,
        nonce=nonce,
        message="Please sign this message with your wallet"
    )


@router.post("/verify", response_model=TokenResponse)
def verify_signature(
    request: SignatureVerification,
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db)
):
    """
    Step 2: Verify wallet signature and return access + refresh tokens.
    Retrieves challenge from cache and validates signature.
    """
    _validate_origin(http_request)

    # Normalize wallet address
    wallet_address = request.wallet_address.lower()
    
    # Retrieve challenge from cache
    challenge_data = challenge_cache.get(wallet_address)
    if not challenge_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending challenge found. Please initiate login first."
        )
    
    # Validate challenge hasn't expired (5 min TTL handled by cache, but double-check)
    challenge = challenge_data["challenge"]
    
    # Verify the signature against the stored challenge
    if not verify_eth_signature(challenge, request.signature, wallet_address):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature"
        )
    
    # Delete challenge from cache (single use)
    challenge_cache.delete(wallet_address)
    
    # Get or create user
    user = db.query(User).filter(User.wallet_address == wallet_address).first()
    if not user:
        user = User(wallet_address=wallet_address)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Update last login
        user.last_login = datetime.now(timezone.utc)
        db.commit()
    
    # Create tokens
    access_token = create_access_token({"sub": user.wallet_address, "user_id": user.id, "is_admin": user.is_admin})
    refresh_token_str = create_refresh_token(
        {"sub": user.wallet_address, "user_id": user.id},
        keep_logged_in=request.keep_logged_in
    )
    
    # Store refresh token in DB
    expire_days = 90 if request.keep_logged_in else settings.refresh_token_expire_days
    expires_at = datetime.now(timezone.utc) + timedelta(days=expire_days)
    
    create_refresh_token_record(
        db,
        user_id=user.id,
        raw_refresh_token=refresh_token_str,
        expires_at=expires_at,
    )
    db.commit()

    refresh_max_age_seconds = expire_days * 24 * 60 * 60
    _set_auth_cookies(
        response=response,
        access_token=access_token,
        refresh_token=refresh_token_str,
        refresh_max_age_seconds=refresh_max_age_seconds,
    )
    
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token_str,
        expires_in=settings.access_token_expire_minutes * 60,
        wallet_address=wallet_address
    )


@router.post("/login-with-key", response_model=TokenResponse)
def login_with_private_key(
    request: PrivateKeyLoginRequest,
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db)
):
    """
    Login directly with private key.
    Derives wallet address, generates challenge, signs it, and returns tokens.
    Also derives CLOB API credentials for Polymarket proxy-wallet balance queries.
    WARNING: Private key should be transmitted over HTTPS only.
    """
    try:
        _validate_origin(http_request)

        # Generate challenge
        nonce = str(uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # Sign the challenge with the private key
        # This also derives the wallet address from the key
        challenge_msg = generate_challenge_message("pending", timestamp, nonce)
        signature, wallet_address = sign_message_with_key(challenge_msg, request.private_key)
        
        # Regenerate challenge with actual wallet address
        challenge = generate_challenge_message(wallet_address, timestamp, nonce)
        signature, wallet_address = sign_message_with_key(challenge, request.private_key)
        
        # Verify signature (sanity check)
        if not verify_eth_signature(challenge, signature, wallet_address):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid private key"
            )
        
        # --- Derive CLOB API credentials for Polymarket balance queries ---
        pk = request.private_key
        if not pk.startswith("0x"):
            pk = "0x" + pk
        
        clob_creds = None
        try:
            from py_clob_client.client import ClobClient
            # Use POLY_PROXY (1) – the most common Polymarket wallet type
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                signature_type=1,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            clob_creds = {
                "api_key": creds.api_key,
                "api_secret": creds.api_secret,
                "api_passphrase": creds.api_passphrase,
            }
        except Exception as creds_err:
            import logging
            logging.getLogger(__name__).warning(
                f"Could not derive CLOB API creds: {creds_err}"
            )
        
        # Store private key + creds in encrypted credential cache.
        store_wallet_credentials(
            wallet_address=wallet_address,
            private_key=pk,
            clob_creds=clob_creds,
        )
        # -----------------------------------------------------------------
        
        # Get or create user
        user = db.query(User).filter(User.wallet_address == wallet_address).first()
        if not user:
            user = User(wallet_address=wallet_address)
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            user.last_login = datetime.now(timezone.utc)
            db.commit()
        
        # Create tokens
        access_token = create_access_token({"sub": user.wallet_address, "user_id": user.id, "is_admin": user.is_admin})
        refresh_token_str = create_refresh_token(
            {"sub": user.wallet_address, "user_id": user.id},
            keep_logged_in=request.keep_logged_in
        )
        
        # Store refresh token
        expire_days = 90 if request.keep_logged_in else settings.refresh_token_expire_days
        expires_at = datetime.now(timezone.utc) + timedelta(days=expire_days)
        
        create_refresh_token_record(
            db,
            user_id=user.id,
            raw_refresh_token=refresh_token_str,
            expires_at=expires_at,
        )
        db.commit()

        refresh_max_age_seconds = expire_days * 24 * 60 * 60
        _set_auth_cookies(
            response=response,
            access_token=access_token,
            refresh_token=refresh_token_str,
            refresh_max_age_seconds=refresh_max_age_seconds,
        )
        
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token_str,
            expires_in=settings.access_token_expire_minutes * 60,
            wallet_address=wallet_address
        )
    except CredentialStoreError as e:
        import logging
        logging.getLogger(__name__).error(
            f"Credential storage failure during login-with-key: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential encryption is not configured correctly. Contact support.",
        )
    except ValueError as e:
        import logging
        logging.getLogger(__name__).error(f"login-with-key ValueError: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid private key format: {str(e)}"
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"login-with-key error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Login failed: {str(e)}"
        )


@router.post("/refresh", response_model=TokenResponse)
def refresh_access_token(
    http_request: Request,
    response: Response,
    request: Optional[TokenRefresh] = None,
    db: Session = Depends(get_db)
):
    """
    Refresh access token using refresh token.
    Only accepts tokens with type='refresh' claim.
    """
    provided_refresh_token = request.refresh_token if request else None
    cookie_refresh_token = http_request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    refresh_token = provided_refresh_token or cookie_refresh_token

    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token required",
        )

    # Origin check is required when browser cookie auth is used.
    if not provided_refresh_token and cookie_refresh_token:
        _validate_origin(http_request)

    # Verify token payload first - must be a refresh token type
    try:
        payload = verify_token(refresh_token)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )
    
    # CRITICAL: Verify this is actually a refresh token, not an access token
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type - refresh token required"
        )
    
    # Verify refresh token is valid and not revoked in DB
    token_record = find_active_refresh_token_record(
        db,
        raw_refresh_token=refresh_token,
    )
    
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token"
        )
    
    # Compare datetimes safely (handle both naive and aware)
    expires_at = token_record.expires_at
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token"
        )
    
    # Create new access token — preserve is_admin from DB (not stale refresh token)
    user = db.query(User).filter(User.id == payload["user_id"]).first()
    is_admin = user.is_admin if user else False
    access_token = create_access_token({"sub": payload["sub"], "user_id": payload["user_id"], "is_admin": is_admin})

    keep_logged_in = bool(
        payload.get("keep_logged_in", False)
        or (expires_at - now).days > settings.refresh_token_expire_days
    )
    refresh_token_str = create_refresh_token(
        {"sub": payload["sub"], "user_id": payload["user_id"]},
        keep_logged_in=keep_logged_in,
    )
    new_expire_days = 90 if keep_logged_in else settings.refresh_token_expire_days
    new_expires_at = datetime.now(timezone.utc) + timedelta(days=new_expire_days)

    token_record.is_revoked = True
    create_refresh_token_record(
        db,
        user_id=token_record.user_id,
        raw_refresh_token=refresh_token_str,
        expires_at=new_expires_at,
    )
    db.commit()

    refresh_max_age_seconds = max(0, int((new_expires_at - now).total_seconds()))
    _set_auth_cookies(
        response=response,
        access_token=access_token,
        refresh_token=refresh_token_str,
        refresh_max_age_seconds=refresh_max_age_seconds,
    )
    
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token_str,
        expires_in=settings.access_token_expire_minutes * 60
    )


@router.post("/logout")
def logout(
    http_request: Request,
    response: Response,
    request: Optional[LogoutRequest] = None,
    db: Session = Depends(get_db),
):
    """
    Logout by revoking refresh token and clearing stored private key.
    """
    provided_refresh_token = request.refresh_token if request else None
    cookie_refresh_token = http_request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    refresh_token = provided_refresh_token or cookie_refresh_token

    if not provided_refresh_token and cookie_refresh_token:
        _validate_origin(http_request)

    if refresh_token:
        token_record = find_active_refresh_token_record(
            db,
            raw_refresh_token=refresh_token,
        )
        
        if token_record:
            token_record.is_revoked = True
            db.commit()

            # Clean up stored private key / CLOB creds for this user
            try:
                user = db.query(User).filter(User.id == token_record.user_id).first()
                if user:
                    delete_wallet_credentials(user.wallet_address)
            except Exception:
                pass

    _clear_auth_cookies(response)
    
    return {"message": "Successfully logged out"}


def get_current_user_from_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
):
    """
    Dependency to extract current user from Authorization header or secure cookie.
    """
    token = _extract_bearer_token(authorization) or request.cookies.get(
        ACCESS_TOKEN_COOKIE_NAME
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_token(token)
    
    # Ensure this is an access token, not a refresh token
    if payload.get("type") == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token required, refresh token provided"
        )
    
    return {
        "user_id": payload.get("user_id"),
        "wallet_address": payload.get("sub"),
        "is_admin": payload.get("is_admin", False),
    }


def get_optional_user_from_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Optional[dict]:
    """
    Like get_current_user_from_token but returns None instead of raising 401
    when no valid credentials are present.  Used by public-facing endpoints
    that optionally enrich responses for authenticated visitors.
    """
    token = _extract_bearer_token(authorization) or request.cookies.get(
        ACCESS_TOKEN_COOKIE_NAME
    )
    if not token:
        return None

    try:
        payload = verify_token(token)
        if payload.get("type") == "refresh":
            return None
        return {
            "user_id": payload.get("user_id"),
            "wallet_address": payload.get("sub"),
            "is_admin": payload.get("is_admin", False),
        }
    except Exception:
        return None


@router.get("/me", response_model=UserResponse)
def get_current_user(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db)
):
    """
    Get current user information.
    Requires valid access token.
    """
    user = db.query(User).filter(User.id == current_user["user_id"]).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return UserResponse(**{
        "id": user.id,
        "wallet_address": user.wallet_address,
        "created_at": user.created_at,
        "last_login": user.last_login,
        "is_admin": user.is_admin,
    })
