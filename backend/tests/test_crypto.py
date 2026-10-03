"""Tests for the Fernet / GCP KMS envelope encryption layer.

Covers keyring rotation (decrypt with any key, encrypt with the
first), the GCP KMS envelope path, misconfiguration errors, and
the ``get_envelope_encryptor`` backend selector.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.security import crypto
from app.security.crypto import (
    EncryptionConfigError,
    FernetEnvelopeEncryptor,
    GcpKmsEnvelopeEncryptor,
    get_envelope_encryptor,
)

KMS_KEY_NAME = "projects/p/locations/l/keyRings/r/cryptoKeys/k"


class FakeKmsClient:
    """Minimal stand-in for google.cloud.kms.KeyManagementServiceClient."""

    def __init__(self):
        self._wrapped = {}
        self._counter = 0

    def encrypt(self, *, name, plaintext):
        self._counter += 1
        handle = f"kms-wrapped-{name}-{self._counter}".encode()
        self._wrapped[handle] = bytes(plaintext)
        return SimpleNamespace(ciphertext=handle)

    def decrypt(self, *, name, ciphertext):
        return SimpleNamespace(plaintext=self._wrapped[bytes(ciphertext)])


class CryptoTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "a" * 32,
                "ENCRYPTION_MASTER_KEYS": Fernet.generate_key().decode(),
                "CREDENTIAL_ENCRYPTION_BACKEND": "fernet",
                "GCP_KMS_KEY_NAME": "",
            },
            clear=False,
        )
        self.env_patcher.start()
        get_settings.cache_clear()
        crypto.get_fernet_keyring.cache_clear()
        get_envelope_encryptor.cache_clear()

    def tearDown(self):
        self.env_patcher.stop()
        get_settings.cache_clear()
        crypto.get_fernet_keyring.cache_clear()
        get_envelope_encryptor.cache_clear()

    def _reconfigure(self, **env):
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        get_settings.cache_clear()
        crypto.get_fernet_keyring.cache_clear()
        get_envelope_encryptor.cache_clear()
        return patcher

    def test_default_backend_is_fernet(self):
        self.assertIsInstance(get_envelope_encryptor(), FernetEnvelopeEncryptor)

    def test_fernet_roundtrip(self):
        ciphertext, kid = crypto.encrypt_text("0xsecret")
        self.assertNotIn("0xsecret", ciphertext)
        self.assertTrue(kid)
        self.assertEqual(crypto.decrypt_text(ciphertext), "0xsecret")

    def test_decrypt_garbage_raises(self):
        with self.assertRaises(InvalidToken):
            crypto.decrypt_text("not-a-token")

    def test_keyring_rotation_keeps_old_values_decryptable(self):
        key_a = Fernet.generate_key().decode()
        key_b = Fernet.generate_key().decode()
        patcher = self._reconfigure(ENCRYPTION_MASTER_KEYS=key_a)
        try:
            token, _ = crypto.encrypt_text("rotated")
        finally:
            patcher.stop()
        # Rotate: key_b becomes primary, key_a stays in the ring for decryption.
        patcher = self._reconfigure(ENCRYPTION_MASTER_KEYS=f"{key_b},{key_a}")
        try:
            self.assertEqual(crypto.decrypt_text(token), "rotated")
        finally:
            patcher.stop()

    def test_unknown_backend_raises(self):
        patcher = self._reconfigure(CREDENTIAL_ENCRYPTION_BACKEND="vault")
        try:
            with self.assertRaises(EncryptionConfigError):
                get_envelope_encryptor()
        finally:
            patcher.stop()

    def test_gcp_kms_requires_key_name(self):
        with self.assertRaises(EncryptionConfigError):
            GcpKmsEnvelopeEncryptor(key_name="")

    def test_gcp_kms_envelope_roundtrip(self):
        fake = FakeKmsClient()
        encryptor = GcpKmsEnvelopeEncryptor(key_name=KMS_KEY_NAME, client_factory=lambda: fake)
        ciphertext, kid = encryptor.encrypt("0xkms-secret")
        self.assertNotIn("0xkms-secret", ciphertext)
        self.assertIn(":", ciphertext)
        self.assertTrue(kid)
        self.assertEqual(encryptor.decrypt(ciphertext), "0xkms-secret")

    def test_gcp_kms_encryption_is_non_deterministic(self):
        fake = FakeKmsClient()
        encryptor = GcpKmsEnvelopeEncryptor(key_name=KMS_KEY_NAME, client_factory=lambda: fake)
        first, _ = encryptor.encrypt("same-value")
        second, _ = encryptor.encrypt("same-value")
        self.assertNotEqual(first, second)
        self.assertEqual(encryptor.decrypt(first), "same-value")
        self.assertEqual(encryptor.decrypt(second), "same-value")

    def test_gcp_kms_backend_selected_without_package(self):
        try:
            import google.cloud.kms  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("google-cloud-kms is installed")
        patcher = self._reconfigure(
            CREDENTIAL_ENCRYPTION_BACKEND="gcp_kms", GCP_KMS_KEY_NAME=KMS_KEY_NAME
        )
        try:
            encryptor = get_envelope_encryptor()
            self.assertIsInstance(encryptor, GcpKmsEnvelopeEncryptor)
            with self.assertRaises(EncryptionConfigError):
                encryptor.encrypt("value")
        finally:
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
