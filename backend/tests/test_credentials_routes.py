"""Tests for the trading-key credential routes under /api/auth/trading-key.

The encrypted credential store is replaced with an in-memory fake so the
tests exercise route behaviour (auth, wallet scoping, response shapes)
without depending on the encryption layer.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.routes.auth import get_current_user_from_token
from app.api.routes.credentials import _trading_key_rate_limit
from app.main import app
from app.security.credential_store import CredentialStoreError

WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20
PRIVATE_KEY = "0x" + "11" * 32


class _FakeCredentialStore:
    """In-memory stand-in for the encrypted credential store."""

    def __init__(self):
        self._entries = {}

    def store_wallet_credentials(
        self,
        wallet_address,
        private_key,
        clob_creds=None,
        ttl_seconds=None,
    ):
        self._entries[wallet_address.lower()] = {
            "private_key": private_key,
            "clob_creds": clob_creds,
        }

    def load_wallet_credentials(self, wallet_address):
        return self._entries.get(wallet_address.lower())

    def delete_wallet_credentials(self, wallet_address):
        return self._entries.pop(wallet_address.lower(), None) is not None


class TradingKeyRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = _FakeCredentialStore()
        self.patchers = [
            patch(
                "app.api.routes.credentials.store_wallet_credentials",
                self.store.store_wallet_credentials,
            ),
            patch(
                "app.api.routes.credentials.load_wallet_credentials",
                self.store.load_wallet_credentials,
            ),
            patch(
                "app.api.routes.credentials.delete_wallet_credentials",
                self.store.delete_wallet_credentials,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        # Keep the 5/minute IP limit out of the tests so request
        # counts stay deterministic.
        app.dependency_overrides[_trading_key_rate_limit] = lambda: None
        self.addCleanup(app.dependency_overrides.clear)

        self.client = TestClient(app)

    def _authenticate(self, wallet=WALLET):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": 1,
            "wallet_address": wallet,
            "is_admin": False,
        }

    def test_post_requires_authentication(self):
        response = self.client.post(
            "/api/auth/trading-key",
            json={"private_key": PRIVATE_KEY},
        )
        self.assertEqual(response.status_code, 401)

    def test_get_requires_authentication(self):
        response = self.client.get("/api/auth/trading-key")
        self.assertEqual(response.status_code, 401)

    def test_delete_requires_authentication(self):
        response = self.client.delete("/api/auth/trading-key")
        self.assertEqual(response.status_code, 401)

    def test_store_then_status_then_delete(self):
        self._authenticate()

        response = self.client.post(
            "/api/auth/trading-key",
            json={
                "private_key": PRIVATE_KEY,
                "clob_credentials": {
                    "api_key": "k",
                    "api_secret": "s",
                    "api_passphrase": "p",
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

        status_response = self.client.get("/api/auth/trading-key")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json(), {"has_trading_key": True})

        delete_response = self.client.delete("/api/auth/trading-key")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json(), {"ok": True})

        status_response = self.client.get("/api/auth/trading-key")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json(), {"has_trading_key": False})

    def test_status_is_false_without_stored_key(self):
        self._authenticate()
        response = self.client.get("/api/auth/trading-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"has_trading_key": False})

    def test_store_uses_session_wallet_not_body(self):
        self._authenticate(wallet=WALLET)
        response = self.client.post(
            "/api/auth/trading-key",
            json={
                "private_key": PRIVATE_KEY,
                # A smuggled wallet address must be ignored: keys can
                # only be stored for the session user's own wallet.
                "wallet_address": OTHER_WALLET,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(self.store.load_wallet_credentials(WALLET))
        self.assertIsNone(self.store.load_wallet_credentials(OTHER_WALLET))

    def test_store_rejects_empty_private_key(self):
        self._authenticate()
        response = self.client.post(
            "/api/auth/trading-key",
            json={"private_key": ""},
        )
        self.assertEqual(response.status_code, 422)

    def test_store_returns_503_when_encryption_unavailable(self):
        self._authenticate()
        with patch(
            "app.api.routes.credentials.store_wallet_credentials",
            side_effect=CredentialStoreError("no keys"),
        ):
            response = self.client.post(
                "/api/auth/trading-key",
                json={"private_key": PRIVATE_KEY},
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured correctly", response.json()["detail"])

    def test_store_returns_400_on_invalid_key_format(self):
        self._authenticate()
        with patch(
            "app.api.routes.credentials.store_wallet_credentials",
            side_effect=ValueError("bad key"),
        ):
            response = self.client.post(
                "/api/auth/trading-key",
                json={"private_key": "not-a-key"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid private key format")

    def test_status_returns_503_when_encryption_unavailable(self):
        self._authenticate()
        with patch(
            "app.api.routes.credentials.load_wallet_credentials",
            side_effect=CredentialStoreError("no keys"),
        ):
            response = self.client.get("/api/auth/trading-key")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured correctly", response.json()["detail"])

    def test_delete_succeeds_without_stored_key(self):
        self._authenticate()
        response = self.client.delete("/api/auth/trading-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
