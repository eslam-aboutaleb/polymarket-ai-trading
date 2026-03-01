"""Authentication and JWT token handling"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from uuid import uuid4
import logging
from jose import JWTError, jwt
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address
from web3 import Web3
from fastapi import HTTPException, status
from app.config import get_settings
import json
import os

settings = get_settings()
logger = logging.getLogger(__name__)

# Load prompts from external JSON file
def load_prompts() -> Dict:
    """Load prompt templates from prompts.json"""
    prompts_path = os.environ.get("PROMPTS_PATH", "prompts/prompts.json")
    try:
        with open(prompts_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # Default prompts if file not found
        return {
            "challenge_message": "Sign this message to authenticate with Polymarket AI Trading:\n\nTimestamp: {timestamp}\nWallet: {wallet_address}\nNonce: {nonce}"
        }

PROMPTS = load_prompts()


def create_access_token(data: Dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token with type='access' claim"""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm
    )
    return encoded_jwt


def create_refresh_token(data: Dict, keep_logged_in: bool = False) -> str:
    """Create refresh token with type='refresh' claim"""
    to_encode = data.copy()
    
    # 90 days if keep_logged_in, else 7 days
    expire_days = 90 if keep_logged_in else 7
    expire = datetime.now(timezone.utc) + timedelta(days=expire_days)
    
    to_encode.update(
        {
            "exp": expire,
            "type": "refresh",
            "jti": str(uuid4()),
            "keep_logged_in": keep_logged_in,
        }
    )
    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm
    )
    return encoded_jwt


def verify_token(token: str) -> Dict:
    """Verify and decode JWT token"""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def generate_challenge_message(wallet_address: str, timestamp: str, nonce: str) -> str:
    """Generate challenge message for user to sign using external prompt template"""
    template = PROMPTS.get("challenge_message", 
        "Sign this message to authenticate with Polymarket AI Trading:\n\nTimestamp: {timestamp}\nWallet: {wallet_address}\nNonce: {nonce}")
    return template.format(
        wallet_address=wallet_address,
        timestamp=timestamp,
        nonce=nonce
    )


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
        recovered_address = w3.eth.account.recover_message(
            signable_message,
            signature=signature
        )
        
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
