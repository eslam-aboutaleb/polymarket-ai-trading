import os
import unittest
from unittest.mock import patch

from app.utils.cache import InMemoryCache, get_cache


class CacheSecurityTests(unittest.TestCase):
    def test_credentials_cache_can_require_redis(self):
        with patch.dict(
            os.environ,
            {
                "ENVIRONMENT": "production",
                "REDIS_URL": "",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                get_cache("credentials", require_redis=True)

    def test_non_sensitive_cache_can_fallback_to_memory(self):
        with patch.dict(
            os.environ,
            {
                "ENVIRONMENT": "production",
                "REDIS_URL": "",
            },
            clear=False,
        ):
            cache = get_cache("challenge", require_redis=False)
            self.assertIsInstance(cache, InMemoryCache)


if __name__ == "__main__":
    unittest.main()
