"""Authentication and JWT token handling"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from fastapi import HTTPException, status
from jose import JWTError, jwt
from web3 import Web3

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# Fallback used when the prompts file is missing or has no template. Kept in one
# place so the two call sites below cannot drift apart.
DEFAULT_CHALLENGE_MESSAGE = (
    "Sign this message to authenticate with Polymarket AI Trading:"
    "\n\nTimestamp: {timestamp}"
    "\nWallet: {wallet_address}"
    "\nNonce: {nonce}"
)


# Load prompts from external JSON file
def load_prompts() -> dict:
    """Load prompt templates from prompts.json"""
    prompts_path = os.environ.get("PROMPTS_PATH", "prompts/prompts.json")
    try:
        with Path(prompts_path).open() as f:
            return json.load(f)
    except FileNotFoundError:
        # Default prompts if file not found
        return {"challenge_message": DEFAULT_CHALLENGE_MESSAGE}


PROMPTS = load_prompts()


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create JWT access token with type='access' claim"""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)

    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(data: dict, keep_logged_in: bool = False) -> str:
    """Create refresh token with type='refresh' claim"""
    to_encode = data.copy()

    # 90 days if keep_logged_in, else 7 days
    expire_days = 90 if keep_logged_in else 7
    expire = datetime.now(UTC) + timedelta(days=expire_days)

    to_encode.update(
        {
            "exp": expire,
            "type": "refresh",
            "jti": str(uuid4()),
            "keep_logged_in": keep_logged_in,
        }
    )
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_token(token: str) -> dict:
    """Verify and decode JWT token"""
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def generate_challenge_message(wallet_address: str, timestamp: str, nonce: str) -> str:
    """Generate challenge message for user to sign using external prompt template"""
    # The template is nested under "authentication" in prompts.json. The flat
    # lookup only matched the synthetic fallback dict built when the file is
    # missing, which meant the packaged template was never actually used.
    template = PROMPTS.get("authentication", {}).get("challenge_message") or PROMPTS.get(
        "challenge_message", DEFAULT_CHALLENGE_MESSAGE
    )
    return template.format(wallet_address=wallet_address, timestamp=timestamp, nonce=nonce)


def verify_eth_signature(message: str, signature: str, address: str) -> bool:
    """
    Verify Ethereum signature using web3.py with proper eth_account API.
    Uses encode_defunct for EIP-191 signed messages (personal_sign).
    """
    try:
        w3 = Web3()

        # Ensure signature is prefixed with 0x
        if not signature.startswith("0x"):
            signature = "0x" + signature

        # Encode message using EIP-191 standard (what personal_sign uses)
        signable_message = encode_defunct(text=message)

        # Recover the address that signed the message
        recovered_address = w3.eth.account.recover_message(signable_message, signature=signature)

        # Normalize both addresses to checksum format for comparison
        expected_address = to_checksum_address(address)
        recovered_address = to_checksum_address(recovered_address)

        return recovered_address.lower() == expected_address.lower()
    except Exception as e:
        logger.warning("Signature verification error: %s", e)
        return False


def sign_message_with_key(message: str, private_key: str) -> tuple[str, str]:
    """
    Sign a message with a private key and return (signature, wallet_address).

    Args:
        message: The message to sign
        private_key: Ethereum private key (with or without 0x prefix)

    Returns:
        Tuple of (signature, wallet_address)
    """
    # Ensure private key has 0x prefix
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    w3 = Web3()
    account = w3.eth.account.from_key(private_key)
    wallet_address = account.address.lower()

    # Sign the message using EIP-191
    signable_message = encode_defunct(text=message)
    signed = account.sign_message(signable_message)
    signature = signed.signature.hex()

    return signature, wallet_address
