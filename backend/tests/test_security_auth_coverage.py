"""Additional coverage for :mod:`app.security.auth`.

Prompt loading (file present / missing), access and refresh token
creation and verification (default and custom expiry, keep_logged_in
flag), challenge-message template resolution (nested, flat, default),
and Ethereum signature verification / signing round-trips using a
well-known test key.
"""

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException
from jose import jwt

from app.config import get_settings
from app.security import auth
from app.security.auth import (
    create_access_token,
    create_refresh_token,
    generate_challenge_message,
    load_prompts,
    sign_message_with_key,
    verify_eth_signature,
    verify_token,
)

# Well-known Ethereum test vector: this private key always maps to
# 0xfe3b557e8fb62b89f4916b721be55ceb828dbd73.
TEST_PRIVATE_KEY = "0x8f2a55949038a9610f50fb23b5883af3b4ecb3c3bb792cbcefbd1542c692be63"
TEST_ADDRESS = "0xfe3b557e8fb62b89f4916b721be55ceb828dbd73"
MESSAGE = "Sign this message to authenticate with Polymarket AI Trading"


class LoadPromptsTests(unittest.TestCase):
    """``load_prompts`` file handling."""

    def test_loads_prompts_from_configured_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"challenge_message": "Custom {nonce}"}, f)
            path = f.name
        self.addCleanup(os.unlink, path)

        with patch.dict(os.environ, {"PROMPTS_PATH": path}):
            prompts = load_prompts()

        self.assertEqual(prompts, {"challenge_message": "Custom {nonce}"})

    def test_missing_file_returns_default_challenge(self):
        with patch.dict(os.environ, {"PROMPTS_PATH": "/nonexistent/prompts.json"}):
            prompts = load_prompts()

        self.assertEqual(prompts, {"challenge_message": auth.DEFAULT_CHALLENGE_MESSAGE})


class TokenCreationTests(unittest.TestCase):
    """``create_access_token`` / ``create_refresh_token`` claims."""

    def setUp(self):
        self.settings = get_settings()

    def _decode(self, token):
        return jwt.decode(
            token, self.settings.jwt_secret_key, algorithms=[self.settings.jwt_algorithm]
        )

    def test_access_token_default_expiry(self):
        before = datetime.now(UTC)
        token = create_access_token({"sub": TEST_ADDRESS, "user_id": 1})
        datetime.now(UTC)

        payload = self._decode(token)
        self.assertEqual(payload["sub"], TEST_ADDRESS)
        self.assertEqual(payload["user_id"], 1)
        self.assertEqual(payload["type"], "access")
        expected = before + timedelta(minutes=self.settings.access_token_expire_minutes)
        self.assertLessEqual(
            abs(datetime.fromtimestamp(payload["exp"], tz=UTC) - expected),
            timedelta(seconds=5),
        )

    def test_access_token_custom_expiry(self):
        before = datetime.now(UTC)
        token = create_access_token({"sub": TEST_ADDRESS}, expires_delta=timedelta(hours=2))

        payload = self._decode(token)
        self.assertEqual(payload["type"], "access")
        expected = before + timedelta(hours=2)
        self.assertLessEqual(
            abs(datetime.fromtimestamp(payload["exp"], tz=UTC) - expected),
            timedelta(seconds=5),
        )

    def test_refresh_token_keep_logged_in_expires_in_ninety_days(self):
        before = datetime.now(UTC)
        token = create_refresh_token({"sub": TEST_ADDRESS}, keep_logged_in=True)

        payload = self._decode(token)
        self.assertEqual(payload["type"], "refresh")
        self.assertTrue(payload["keep_logged_in"])
        self.assertIn("jti", payload)
        expected = before + timedelta(days=90)
        self.assertLessEqual(
            abs(datetime.fromtimestamp(payload["exp"], tz=UTC) - expected),
            timedelta(seconds=5),
        )

    def test_refresh_token_default_expires_in_seven_days(self):
        before = datetime.now(UTC)
        token = create_refresh_token({"sub": TEST_ADDRESS})

        payload = self._decode(token)
        self.assertFalse(payload["keep_logged_in"])
        expected = before + timedelta(days=7)
        self.assertLessEqual(
            abs(datetime.fromtimestamp(payload["exp"], tz=UTC) - expected),
            timedelta(seconds=5),
        )

    def test_refresh_token_jtis_are_unique(self):
        first = self._decode(create_refresh_token({"sub": TEST_ADDRESS}))
        second = self._decode(create_refresh_token({"sub": TEST_ADDRESS}))
        self.assertNotEqual(first["jti"], second["jti"])


class VerifyTokenTests(unittest.TestCase):
    def test_valid_token_roundtrips(self):
        token = create_access_token({"sub": TEST_ADDRESS, "user_id": 42})
        payload = verify_token(token)
        self.assertEqual(payload["sub"], TEST_ADDRESS)
        self.assertEqual(payload["user_id"], 42)

    def test_garbage_token_raises_401(self):
        with self.assertRaises(HTTPException) as ctx:
            verify_token("not-a-jwt")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Invalid authentication credentials")
        self.assertEqual(ctx.exception.headers, {"WWW-Authenticate": "Bearer"})

    def test_token_signed_with_wrong_key_raises_401(self):
        forged = jwt.encode({"sub": TEST_ADDRESS}, "wrong-secret", algorithm="HS256")
        with self.assertRaises(HTTPException) as ctx:
            verify_token(forged)
        self.assertEqual(ctx.exception.status_code, 401)


class ChallengeMessageTests(unittest.TestCase):
    def test_nested_authentication_template_is_used(self):
        prompts = {
            "authentication": {
                "challenge_message": "Sign {wallet_address} ts={timestamp} n={nonce}"
            }
        }
        with patch.object(auth, "PROMPTS", prompts):
            message = generate_challenge_message("0xabc", "2024-01-01T00:00:00Z", "n-1")
        self.assertEqual(message, "Sign 0xabc ts=2024-01-01T00:00:00Z n=n-1")

    def test_flat_template_is_used_as_fallback(self):
        prompts = {"challenge_message": "Flat {wallet_address} {nonce}"}
        with patch.object(auth, "PROMPTS", prompts):
            message = generate_challenge_message("0xabc", "ts", "n-2")
        self.assertEqual(message, "Flat 0xabc n-2")

    def test_default_template_when_no_template_present(self):
        with patch.object(auth, "PROMPTS", {}):
            message = generate_challenge_message("0xabc", "2024-01-01", "n-3")
        self.assertIn("Wallet: 0xabc", message)
        self.assertIn("Timestamp: 2024-01-01", message)
        self.assertIn("Nonce: n-3", message)


class EthSignatureTests(unittest.TestCase):
    def test_sign_then_verify_roundtrips(self):
        signature, wallet_address = sign_message_with_key(MESSAGE, TEST_PRIVATE_KEY)
        self.assertEqual(wallet_address, TEST_ADDRESS.lower())
        self.assertTrue(verify_eth_signature(MESSAGE, signature, TEST_ADDRESS))

    def test_verify_accepts_signature_without_0x_prefix(self):
        # sign_message_with_key returns a bare hex signature (no 0x
        # prefix); verification must accept it as-is.
        signature, _ = sign_message_with_key(MESSAGE, TEST_PRIVATE_KEY)
        self.assertFalse(signature.startswith("0x"))
        self.assertTrue(verify_eth_signature(MESSAGE, signature, TEST_ADDRESS))

    def test_verify_rejects_wrong_address(self):
        signature, _ = sign_message_with_key(MESSAGE, TEST_PRIVATE_KEY)
        other_wallet = "0x" + "cd" * 20
        self.assertFalse(verify_eth_signature(MESSAGE, signature, other_wallet))

    def test_verify_rejects_garbage_signature(self):
        self.assertFalse(verify_eth_signature(MESSAGE, "0xdeadbeef", TEST_ADDRESS))

    def test_verify_rejects_empty_signature(self):
        self.assertFalse(verify_eth_signature(MESSAGE, "", TEST_ADDRESS))

    def test_sign_message_with_key_accepts_bare_private_key(self):
        signature, wallet_address = sign_message_with_key(MESSAGE, TEST_PRIVATE_KEY[2:])
        self.assertEqual(wallet_address, TEST_ADDRESS.lower())
        self.assertTrue(verify_eth_signature(MESSAGE, signature, TEST_ADDRESS))


if __name__ == "__main__":
    unittest.main()
