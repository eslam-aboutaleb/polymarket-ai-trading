"""Additional coverage for :mod:`app.security.credential_store`.

Complements tests/test_credential_store.py: payload helpers
(``_normalize_clob_creds``, ``_encrypt_payload``, ``_decrypt_payload``,
payload-shape predicates), every ``store`` / ``load`` / ``delete``
branch (missing key, undecryptable entry, encryption misconfiguration,
legacy migration failure, unknown payload shape), metrics, and audit
logging.
"""

import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.security import credential_store
from app.security.credential_store import (
    CredentialStoreError,
    delete_wallet_credentials,
    get_credential_store_metrics,
    load_wallet_credentials,
    store_wallet_credentials,
)
from app.security.crypto import (
    EncryptionConfigError,
    get_envelope_encryptor,
    get_fernet_keyring,
)
from app.utils.cache import InMemoryCache

WALLET = "0xAbC123"


class CredentialStoreCoverageTests(unittest.TestCase):
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
        get_envelope_encryptor.cache_clear()

        self.cache = InMemoryCache("credentials-coverage-test")
        self.cache_patcher = patch("app.security.credential_store.key_store", self.cache)
        self.cache_patcher.start()

        # Module-level counters accumulate across tests; reset
        # them so assertions stay order-independent.
        credential_store._metrics["plaintext_migrated"] = 0

    def tearDown(self):
        self.cache_patcher.stop()
        self.env_patcher.stop()
        get_settings.cache_clear()
        get_fernet_keyring.cache_clear()
        get_envelope_encryptor.cache_clear()

    # ── Payload helpers ───────────────────────────────

    def test_normalize_clob_creds_variants(self):
        self.assertIsNone(credential_store._normalize_clob_creds(None))
        self.assertIsNone(credential_store._normalize_clob_creds("not-a-dict"))
        self.assertIsNone(credential_store._normalize_clob_creds({}))
        self.assertEqual(
            credential_store._normalize_clob_creds({"api_key": "k"}),
            {"api_key": "k"},
        )
        # Empty-string values are dropped.
        self.assertEqual(
            credential_store._normalize_clob_creds({"api_key": "k", "api_secret": ""}),
            {"api_key": "k"},
        )
        # Non-string values are dropped.
        self.assertIsNone(credential_store._normalize_clob_creds({"api_key": 123}))

    def test_encrypt_payload_without_creds(self):
        payload = credential_store._encrypt_payload("0xpk", None)
        self.assertEqual(payload["v"], credential_store.CREDENTIAL_ENVELOPE_VERSION)
        self.assertIn("kid", payload)
        self.assertIn("private_key_enc", payload)
        self.assertIsNone(payload["clob_creds_enc"])
        self.assertIn("stored_at", payload)

    def test_encrypt_payload_with_creds(self):
        payload = credential_store._encrypt_payload("0xpk", {"api_key": "k", "api_secret": "s"})
        self.assertIsInstance(payload["clob_creds_enc"], dict)
        self.assertIn("api_key", payload["clob_creds_enc"])
        self.assertIn("api_secret", payload["clob_creds_enc"])
        # Ciphertext must not equal the plaintext (base64 may
        # randomly contain single characters, so compare whole values).
        self.assertNotEqual(payload["clob_creds_enc"]["api_key"], "k")
        self.assertNotEqual(payload["clob_creds_enc"]["api_secret"], "s")

    def test_decrypt_payload_missing_private_key_raises(self):
        with self.assertRaises(InvalidToken):
            credential_store._decrypt_payload({"v": 1})

    def test_decrypt_payload_empty_creds_dict_becomes_none(self):
        payload = credential_store._encrypt_payload("0xpk", None)
        payload["clob_creds_enc"] = {}
        credentials = credential_store._decrypt_payload(payload)
        self.assertEqual(credentials["private_key"], "0xpk")
        self.assertIsNone(credentials["clob_creds"])

    def test_decrypt_payload_skips_non_string_cred_values(self):
        payload = credential_store._encrypt_payload("0xpk", None)
        payload["clob_creds_enc"] = {"api_key": 123, "api_secret": ""}
        credentials = credential_store._decrypt_payload(payload)
        self.assertIsNone(credentials["clob_creds"])

    def test_payload_shape_predicates(self):
        envelope = credential_store._encrypt_payload("0xpk", None)
        self.assertTrue(credential_store._is_encrypted_payload(envelope))
        self.assertFalse(credential_store._is_encrypted_payload({"v": 2, "private_key_enc": "x"}))
        self.assertFalse(credential_store._is_encrypted_payload({"private_key_enc": "x"}))
        self.assertFalse(credential_store._is_encrypted_payload("not-a-dict"))

        self.assertTrue(credential_store._is_legacy_plaintext_payload({"private_key": "x"}))
        self.assertFalse(credential_store._is_legacy_plaintext_payload({"private_key": 123}))
        self.assertFalse(credential_store._is_legacy_plaintext_payload({}))
        self.assertFalse(credential_store._is_legacy_plaintext_payload(None))

    # ── store_wallet_credentials ──────────────────────

    def test_store_requires_private_key(self):
        with self.assertRaises(CredentialStoreError):
            store_wallet_credentials(WALLET, "")

    def test_store_wraps_encryption_config_error(self):
        with (
            patch.object(
                credential_store,
                "encrypt_text",
                side_effect=EncryptionConfigError("no keys"),
            ),
            self.assertRaises(CredentialStoreError),
        ):
            store_wallet_credentials(WALLET, "0xpk")

    def test_store_uses_default_ttl_when_none(self):
        store_wallet_credentials(WALLET, "0xpk")
        entry = self.cache._cache.get(f"credentials-coverage-test:pk:{WALLET.lower()}")
        self.assertIsNotNone(entry)
        # Default TTL comes from settings (30 days).
        self.assertGreater(entry["expires_at"], 0)

    # ── load_wallet_credentials ───────────────────────

    def test_load_missing_returns_none(self):
        self.assertIsNone(load_wallet_credentials(WALLET))

    def test_load_roundtrip(self):
        store_wallet_credentials(WALLET, "0xpk", {"api_key": "k", "api_secret": "s"})
        loaded = load_wallet_credentials(WALLET)
        self.assertEqual(loaded["private_key"], "0xpk")
        self.assertEqual(loaded["clob_creds"]["api_key"], "k")
        self.assertEqual(loaded["clob_creds"]["api_secret"], "s")

    def test_load_undecryptable_entry_is_deleted(self):
        store_wallet_credentials(WALLET, "0xpk")
        key = f"pk:{WALLET.lower()}"

        with patch.object(credential_store, "decrypt_text", side_effect=InvalidToken("bad")):
            self.assertIsNone(load_wallet_credentials(WALLET))

        self.assertIsNone(self.cache.get(key))
        self.assertEqual(get_credential_store_metrics()["decrypt_failed"], 1)

    def test_load_encryption_config_error_raises(self):
        store_wallet_credentials(WALLET, "0xpk")

        with (
            patch.object(
                credential_store,
                "decrypt_text",
                side_effect=EncryptionConfigError("no keys"),
            ),
            self.assertRaises(CredentialStoreError),
        ):
            load_wallet_credentials(WALLET)

    def test_load_legacy_plaintext_is_migrated(self):
        self.cache.set(
            f"pk:{WALLET.lower()}",
            {"private_key": "0xlegacy", "clob_creds": {"api_key": "legacy-k"}},
            ttl_seconds=3600,
        )

        loaded = load_wallet_credentials(WALLET)

        self.assertEqual(loaded["private_key"], "0xlegacy")
        self.assertEqual(loaded["clob_creds"], {"api_key": "legacy-k"})
        self.assertEqual(get_credential_store_metrics()["plaintext_migrated"], 1)
        # The stored payload is now an encrypted envelope.
        migrated = self.cache.get(f"pk:{WALLET.lower()}")
        self.assertTrue(credential_store._is_encrypted_payload(migrated))

    def test_load_legacy_migration_failure_deletes_and_raises(self):
        self.cache.set(
            f"pk:{WALLET.lower()}",
            {"private_key": "0xlegacy", "clob_creds": None},
            ttl_seconds=3600,
        )

        with (
            patch.object(
                credential_store,
                "store_wallet_credentials",
                side_effect=CredentialStoreError("keystore locked"),
            ),
            self.assertRaises(CredentialStoreError),
        ):
            load_wallet_credentials(WALLET)

        self.assertIsNone(self.cache.get(f"pk:{WALLET.lower()}"))

    def test_load_unknown_payload_shape_is_deleted(self):
        self.cache.set(f"pk:{WALLET.lower()}", {"foo": "bar"}, ttl_seconds=3600)

        self.assertIsNone(load_wallet_credentials(WALLET))
        self.assertIsNone(self.cache.get(f"pk:{WALLET.lower()}"))

    # ── delete / metrics / audit ──────────────────────

    def test_delete_returns_true_for_existing_entry(self):
        store_wallet_credentials(WALLET, "0xpk")
        self.assertTrue(delete_wallet_credentials(WALLET))
        self.assertIsNone(load_wallet_credentials(WALLET))

    def test_delete_returns_false_for_missing_entry(self):
        self.assertFalse(delete_wallet_credentials(WALLET))

    def test_metrics_snapshot_is_a_copy(self):
        metrics = get_credential_store_metrics()
        self.assertEqual(set(metrics), {"plaintext_migrated", "decrypt_failed"})
        metrics["plaintext_migrated"] = 999
        self.assertNotEqual(get_credential_store_metrics()["plaintext_migrated"], 999)

    def test_audit_records_event_and_wallet(self):
        with patch.object(credential_store, "audit_logger") as audit_mock:
            credential_store._audit("credentials_stored", WALLET)

        audit_mock.info.assert_called_once()
        args, _kwargs = audit_mock.info.call_args
        self.assertIn("event=%s wallet=%s caller=%s", args)
        self.assertIn("credentials_stored", args)
        self.assertIn(WALLET, args)


if __name__ == "__main__":
    unittest.main()
