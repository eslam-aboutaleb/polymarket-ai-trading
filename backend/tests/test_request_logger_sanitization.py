import importlib
import os
import unittest
from unittest.mock import patch

from app.config import get_settings


class RequestLoggerSanitizationTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "a" * 32,
                "DEBUG_LOG_HASH_SALT": "unit-test-salt",
            },
            clear=False,
        )
        self.env_patcher.start()
        get_settings.cache_clear()

        import app.middleware.request_logger as request_logger

        self.request_logger = importlib.reload(request_logger)

    def tearDown(self):
        self.env_patcher.stop()
        get_settings.cache_clear()

    def test_sensitive_query_params_are_redacted(self):
        redacted = self.request_logger._sanitize_query(
            "token=abc&market=foo&api_key=xyz&limit=10"
        )
        self.assertIn("token=%2A%2A%2A", redacted)
        self.assertIn("api_key=%2A%2A%2A", redacted)
        self.assertIn("market=foo", redacted)
        self.assertIn("limit=10", redacted)

    def test_client_ip_is_hashed(self):
        hashed_one = self.request_logger._hash_client_ip("192.168.1.22")
        hashed_two = self.request_logger._hash_client_ip("192.168.1.22")
        self.assertEqual(hashed_one, hashed_two)
        self.assertEqual(len(hashed_one), 16)
        self.assertNotEqual(hashed_one, "192.168.1.22")


if __name__ == "__main__":
    unittest.main()
