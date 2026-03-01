import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.config import get_settings
from app.security.credential_store import load_wallet_credentials, store_wallet_credentials
from app.security.crypto import get_fernet_keyring
from app.utils.cache import InMemoryCache


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "a" * 32,
                "ENCRYPTION_MASTER_KEYS": Fernet.generate_key().decode(),
            },
            clear=False,
        )
        self.env_patcher.start()
        get_settings.cache_clear()
        get_fernet_keyring.cache_clear()

        self.cache = InMemoryCache("credentials-test")
        self.cache_patcher = patch("app.security.credential_store.key_store", self.cache)
        self.cache_patcher.start()

    def tearDown(self):
        self.cache_patcher.stop()
        self.env_patcher.stop()
        get_settings.cache_clear()
        get_fernet_keyring.cache_clear()

    def test_store_and_load_roundtrip(self):
        wallet = "0xabc123"
        store_wallet_credentials(
            wallet_address=wallet,
            private_key="0xprivate",
            clob_creds={"api_key": "k", "api_secret": "s", "api_passphrase": "p"},
            ttl_seconds=3600,
        )

        loaded = load_wallet_credentials(wallet)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["private_key"], "0xprivate")
        self.assertEqual(loaded["clob_creds"]["api_key"], "k")

        raw = self.cache.get(f"pk:{wallet.lower()}")
        self.assertEqual(raw["v"], 1)
        self.assertIn("private_key_enc", raw)
        self.assertNotIn("private_key", raw)

    def test_plaintext_payload_is_migrated(self):
        wallet = "0xdef456"
        self.cache.set(
            f"pk:{wallet.lower()}",
            {
                "private_key": "0xlegacy",
                "clob_creds": {
                    "api_key": "legacy-key",
                    "api_secret": "legacy-secret",
                    "api_passphrase": "legacy-pass",
                },
            },
            ttl_seconds=3600,
        )

        loaded = load_wallet_credentials(wallet)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["private_key"], "0xlegacy")

        migrated = self.cache.get(f"pk:{wallet.lower()}")
        self.assertEqual(migrated["v"], 1)
        self.assertIn("private_key_enc", migrated)
        self.assertNotIn("private_key", migrated)


if __name__ == "__main__":
    unittest.main()
