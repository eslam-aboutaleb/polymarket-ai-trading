import unittest
from unittest.mock import MagicMock, patch

from app.utils.cache import InMemoryCache
import app.utils.cache as cache_module


class CachePerformanceTests(unittest.TestCase):
    def test_in_memory_cache_evicts_lru_entries(self):
        cache = InMemoryCache("perf-test", max_size=2)
        cache.set("a", {"v": 1}, ttl_seconds=300)
        cache.set("b", {"v": 2}, ttl_seconds=300)

        # Touch "a" so "b" becomes the least-recently-used entry.
        self.assertEqual(cache.get("a"), {"v": 1})
        cache.set("c", {"v": 3}, ttl_seconds=300)

        self.assertEqual(cache.get("a"), {"v": 1})
        self.assertEqual(cache.get("c"), {"v": 3})
        self.assertIsNone(cache.get("b"))

    def test_in_memory_cache_respects_ttl_expiry(self):
        cache = InMemoryCache("ttl-test", max_size=10)
        cache.set("expired", {"ok": False}, ttl_seconds=-1)
        self.assertIsNone(cache.get("expired"))

    def test_json_serializer_fallback_without_orjson(self):
        payload = {"alpha": 1, "beta": [1, 2, 3]}
        with patch.object(cache_module, "ORJSON_AVAILABLE", False), patch.object(
            cache_module, "_orjson", None
        ):
            dumped = cache_module._json_dumps(payload)
            self.assertIsInstance(dumped, str)
            self.assertEqual(cache_module._json_loads(dumped), payload)

    def test_json_serializer_orjson_path(self):
        fake_orjson = MagicMock()
        fake_orjson.dumps.return_value = b'{"ok":true}'
        fake_orjson.loads.return_value = {"ok": True}

        with patch.object(cache_module, "ORJSON_AVAILABLE", True), patch.object(
            cache_module, "_orjson", fake_orjson
        ):
            dumped = cache_module._json_dumps({"ok": True})
            self.assertEqual(dumped, '{"ok":true}')
            self.assertEqual(cache_module._json_loads(dumped), {"ok": True})
            fake_orjson.dumps.assert_called_once()
            fake_orjson.loads.assert_called_once_with('{"ok":true}')


if __name__ == "__main__":
    unittest.main()
