"""Trading-key credential routes, decoupled from login.

Login is signature-only; the wallet private key is collected here, in a
separate explicit "enable auto-trading" step, and only for users who run
automated strategies (stop-loss, inverse bot, latency arb). The wallet
address always comes from the authenticated session — a client can never
store a key for another wallet.
"""

import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.routes.auth.auth import get_current_user_from_token
from app.schemas.auth import PRIVATE_KEY_PATTERN
from app.security.credential_store import (
    CredentialStoreError,
    delete_wallet_credentials,
    load_wallet_credentials,
    store_wallet_credentials,
)
from app.security.rate_limit import limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class TradingKeyRequest(BaseModel):
    """Store a trading private key for the session user's own wallet."""

    private_key: str = Field(min_length=1, description="Wallet private key")
    signature_type: Literal[0, 1, 2, 3] | None = None
    funder: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{40}$")
    clob_credentials: dict[str, str] | None = Field(
        None,
        description="Optional CLOB API credentials (api_key/api_secret/api_passphrase)",
    )


def _validate_private_key_format(private_key: str) -> None:
    """Reject malformed keys before they reach the credential store."""
    if not re.fullmatch(PRIVATE_KEY_PATTERN, private_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid private key format",
        )


# Applied as a FastAPI dependency (rather than a decorator on the handler)
# so the handler signature stays clean; the limiter keys off the client IP.
@limiter.limit("5/minute")
def _trading_key_rate_limit(request: Request) -> None:
    """Rate-limit POST /api/auth/trading-key: 5/minute per IP."""


@router.post(
    "/trading-key",
    dependencies=[Depends(_trading_key_rate_limit)],
)
def store_trading_key(
    request: TradingKeyRequest,
    current_user: dict = Depends(get_current_user_from_token),
):
    """
    Store a trading private key for the authenticated user's wallet.

    The wallet address is taken from the session token, never from the
    request body. Used by the explicit "enable auto-trading" step.
    """
    wallet_address = current_user["wallet_address"]
    # Reject a malformed key before it is encrypted and persisted.
    _validate_private_key_format(request.private_key)
    if request.signature_type not in (None, 0) and not request.funder:
        raise HTTPException(422, "Funding wallet is required for this wallet type")
    if request.funder and request.signature_type is None:
        raise HTTPException(422, "Select a signature type for the funding wallet")
    identity = {}
    if request.signature_type is not None:
        identity = {"signature_type": request.signature_type, "funder": request.funder}
    try:
        store_wallet_credentials(
            wallet_address=wallet_address,
            private_key=request.private_key,
            clob_creds=request.clob_credentials,
            **identity,
        )
    except CredentialStoreError as e:
        logger.error(
            "Trading-key storage failure for wallet=%s",
            wallet_address,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential encryption is not configured correctly. Contact support.",
        ) from e
    except ValueError as e:
        logger.error("Trading-key storage ValueError: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid private key format",
        ) from e
    return {"ok": True}


@router.get("/trading-key")
def get_trading_key_status(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Report whether the session user has a trading key stored.

    Never returns key material or any credential bytes.
    """
    wallet_address = current_user["wallet_address"]
    try:
        credentials = load_wallet_credentials(wallet_address)
    except CredentialStoreError as e:
        logger.error(
            "Trading-key load failure for wallet=%s",
            wallet_address,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential encryption is not configured correctly. Contact support.",
        ) from e
    return {"has_trading_key": credentials is not None}


@router.delete("/trading-key")
def delete_trading_key(
    current_user: dict = Depends(get_current_user_from_token),
):
    """Delete the session user's stored trading key and CLOB credentials."""
    wallet_address = current_user["wallet_address"]
    delete_wallet_credentials(wallet_address)
    return {"ok": True}
