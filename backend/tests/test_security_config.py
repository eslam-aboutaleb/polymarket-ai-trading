import unittest

from app.config import Settings


class SettingsValidationTests(unittest.TestCase):
    def test_jwt_secret_rejects_placeholder(self):
        with self.assertRaises(ValueError):
            Settings(jwt_secret_key="CHANGE-ME-IN-PRODUCTION")

    def test_jwt_secret_rejects_short_value(self):
        with self.assertRaises(ValueError):
            Settings(jwt_secret_key="short-secret")

    def test_debug_salt_required_when_debug_enabled(self):
        with self.assertRaises(ValueError):
            Settings(
                jwt_secret_key="a" * 32,
                debug_endpoints_enabled=True,
                debug_log_hash_salt="",
            )

    def test_valid_security_configuration(self):
        settings = Settings(
            jwt_secret_key="a" * 32,
            debug_endpoints_enabled=True,
            debug_log_hash_salt="salt",
            credential_cache_ttl_seconds=3600,
        )
        self.assertEqual(settings.jwt_secret_key, "a" * 32)


if __name__ == "__main__":
    unittest.main()
