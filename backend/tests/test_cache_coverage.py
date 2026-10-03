"""Additional coverage for ``app.utils.cache``.

Complements test_cache_performance.py / test_cache_security.py with the
environment helpers, the InMemoryCache internals (namespace keys, expiry
sweep, max-size enforcement, clamping, settings fallback), the full
RedisCache surface against a fake redis client, and every branch of the
``get_cache`` factory (redis up, redis down, redis required, package
missing).
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import app.utils.cache as cache_module
from app.utils.cache import InMemoryCache, RedisCache, get_cache


class EnvironmentHelperTests(unittest.TestCase):
    def test_is_local_environment_accepts_local_variants(self):
        for value in ("dev", "development", "local", "test", "DEV", " Test "):
            with patch.dict(os.environ, {"ENVIRONMENT": value}):
                self.assertTrue(cache_module._is_local_environment(), value)

    def test_is_local_environment_rejects_production(self):
        for value in ("production", "staging", ""):
            with patch.dict(os.environ, {"ENVIRONMENT": value}):
                self.assertFalse(cache_module._is_local_environment(), value)

    def test_is_local_environment_defaults_to_development(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            self.assertTrue(cache_module._is_local_environment())

    def test_bool_env_unset_returns_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CACHE_TEST_VAR", None)
            self.assertTrue(cache_module._bool_env("CACHE_TEST_VAR", True))
            self.assertFalse(cache_module._bool_env("CACHE_TEST_VAR", False))

    def test_bool_env_truthy_values(self):
        for value in ("1", "true", "TRUE", "yes", "on", " on "):
            with patch.dict(os.environ, {"CACHE_TEST_VAR": value}):
                self.assertTrue(cache_module._bool_env("CACHE_TEST_VAR", False), value)

    def test_bool_env_falsy_values(self):
        for value in ("0", "false", "no", "off", "", "garbage"):
            with patch.dict(os.environ, {"CACHE_TEST_VAR": value}):
                self.assertFalse(cache_module._bool_env("CACHE_TEST_VAR", True), value)


class InMemoryCacheInternalTests(unittest.TestCase):
    def test_namespace_key(self):
        cache = InMemoryCache("ns")
        self.assertEqual(cache._key("k"), "ns:k")

    def test_max_size_clamped_to_one(self):
        self.assertEqual(InMemoryCache("ns", max_size=0)._max_size, 1)
        self.assertEqual(InMemoryCache("ns", max_size=-5)._max_size, 1)

    def test_max_size_from_settings(self):
        fake_settings = MagicMock()
        fake_settings.in_memory_cache_max_entries = 42
        with patch("app.config.get_settings", return_value=fake_settings):
            cache = InMemoryCache("ns")
        self.assertEqual(cache._max_size, 42)

    def test_max_size_falls_back_when_settings_fail(self):
        with patch("app.config.get_settings", side_effect=RuntimeError("no config")):
            cache = InMemoryCache("ns")
        self.assertEqual(cache._max_size, 10000)

    def test_set_overwrites_and_keeps_single_entry(self):
        cache = InMemoryCache("ns", max_size=10)
        cache.set("k", 1, ttl_seconds=300)
        cache.set("k", 2, ttl_seconds=300)
        self.assertEqual(cache.get("k"), 2)
        self.assertEqual(len(cache._cache), 1)

    def test_evict_expired_removes_only_expired_entries(self):
        cache = InMemoryCache("ns", max_size=10)
        cache.set("fresh", 1, ttl_seconds=300)
        cache.set("stale", 2, ttl_seconds=300)
        cache._cache[cache._key("stale")]["expires_at"] = 0.0
        cache._evict_expired()
        self.assertEqual(cache.get("fresh"), 1)
        self.assertIsNone(cache.get("stale"))
        self.assertNotIn(cache._key("stale"), cache._cache)

    def test_enforce_max_size_drops_oldest_entries(self):
        cache = InMemoryCache("ns", max_size=2)
        cache.set("a", 1, ttl_seconds=300)
        cache.set("b", 2, ttl_seconds=300)
        cache.set("c", 3, ttl_seconds=300)
        self.assertEqual(len(cache._cache), 2)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        self.assertEqual(cache.get("c"), 3)

    def test_delete_returns_false_for_missing_key(self):
        cache = InMemoryCache("ns", max_size=10)
        cache.set("k", 1, ttl_seconds=300)
        self.assertTrue(cache.delete("k"))
        self.assertFalse(cache.delete("k"))

    def test_clear_removes_all_entries(self):
        cache = InMemoryCache("ns", max_size=10)
        cache.set("a", 1, ttl_seconds=300)
        cache.set("b", 2, ttl_seconds=300)
        cache.clear()
        self.assertEqual(len(cache._cache), 0)
        self.assertIsNone(cache.get("a"))

    def test_expired_entry_is_removed_on_read(self):
        cache = InMemoryCache("ns", max_size=10)
        cache.set("k", 1, ttl_seconds=-1)
        self.assertIsNone(cache.get("k"))
        self.assertNotIn(cache._key("k"), cache._cache)


class _FakeRedisClient:
    """Minimal stand-in for a redis client (decode_responses=True)."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.setex_calls: list[tuple[str, int, str]] = []
        self.deleted: list[tuple[str, ...]] = []
        self.ping_calls = 0

    def ping(self):
        self.ping_calls += 1

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        count = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                count += 1
        self.deleted.append(keys)
        return count

    def keys(self, pattern):
        prefix = pattern[: pattern.index("*")]
        return [key for key in self.store if key.startswith(prefix)]


class RedisCacheTests(unittest.TestCase):
    def setUp(self):
        self.fake_redis = _FakeRedisClient()
        patcher = patch.object(cache_module.redis, "from_url", return_value=self.fake_redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = RedisCache("redis://fake:6379/0", "ns")

    def test_prefix_uses_polymarket_namespace(self):
        self.assertEqual(self.cache._prefix, "polymarket:ns:")

    def test_set_stores_json_with_ttl(self):
        self.cache.set("k", {"a": 1}, ttl_seconds=42)
        key, ttl, value = self.fake_redis.setex_calls[0]
        self.assertEqual(key, "polymarket:ns:k")
        self.assertEqual(ttl, 42)
        self.assertEqual(self.fake_redis.store["polymarket:ns:k"], value)

    def test_get_returns_deserialized_value(self):
        self.cache.set("k", {"a": [1, 2]})
        self.assertEqual(self.cache.get("k"), {"a": [1, 2]})

    def test_get_returns_none_for_missing_key(self):
        self.assertIsNone(self.cache.get("missing"))

    def test_delete_returns_bool_of_redis_reply(self):
        self.cache.set("k", 1)
        self.assertTrue(self.cache.delete("k"))
        self.assertFalse(self.cache.delete("k"))

    def test_clear_deletes_all_prefixed_keys(self):
        self.cache.set("a", 1)
        self.cache.set("b", 2)
        self.cache.clear()
        self.assertEqual(self.fake_redis.store, {})
        self.assertEqual(self.fake_redis.deleted[-1], ("polymarket:ns:a", "polymarket:ns:b"))

    def test_clear_without_keys_skips_delete(self):
        self.cache.clear()
        self.assertEqual(self.fake_redis.deleted, [])

    def test_redis_cache_requires_redis_package(self):
        with (
            patch.object(cache_module, "REDIS_AVAILABLE", False),
            self.assertRaises(ImportError) as ctx,
        ):
            RedisCache("redis://fake:6379/0", "ns")
        self.assertIn("redis package not installed", str(ctx.exception))


class GetCacheFactoryTests(unittest.TestCase):
    def test_no_redis_url_returns_in_memory_cache(self):
        with patch.dict(os.environ, {"REDIS_URL": ""}):
            cache = get_cache("challenge")
        self.assertIsInstance(cache, InMemoryCache)

    def test_redis_url_with_live_redis_returns_redis_cache(self):
        fake_redis = _FakeRedisClient()
        with (
            patch.dict(os.environ, {"REDIS_URL": "redis://fake:6379/0"}),
            patch.object(cache_module.redis, "from_url", return_value=fake_redis),
        ):
            cache = get_cache("challenge")
        self.assertIsInstance(cache, RedisCache)
        self.assertEqual(fake_redis.ping_calls, 1)

    def test_redis_url_with_live_redis_and_require_redis(self):
        fake_redis = _FakeRedisClient()
        with (
            patch.dict(os.environ, {"REDIS_URL": "redis://fake:6379/0"}),
            patch.object(cache_module.redis, "from_url", return_value=fake_redis),
        ):
            cache = get_cache("challenge", require_redis=True)
        self.assertIsInstance(cache, RedisCache)

    def test_redis_failure_falls_back_to_in_memory(self):
        fake_redis = MagicMock()
        fake_redis.ping.side_effect = RuntimeError("redis down")
        with (
            patch.dict(os.environ, {"REDIS_URL": "redis://fake:6379/0"}),
            patch.object(cache_module.redis, "from_url", return_value=fake_redis),
        ):
            cache = get_cache("challenge")
        self.assertIsInstance(cache, InMemoryCache)

    def test_redis_failure_with_require_redis_raises(self):
        fake_redis = MagicMock()
        fake_redis.ping.side_effect = RuntimeError("redis down")
        with (
            patch.dict(os.environ, {"REDIS_URL": "redis://fake:6379/0"}),
            patch.object(cache_module.redis, "from_url", return_value=fake_redis),
            self.assertRaises(RuntimeError) as ctx,
        ):
            get_cache("challenge", require_redis=True)
        self.assertIn("Redis connection failed", str(ctx.exception))

    def test_require_redis_without_package_raises(self):
        with (
            patch.dict(os.environ, {"REDIS_URL": "redis://fake:6379/0"}),
            patch.object(cache_module, "REDIS_AVAILABLE", False),
            self.assertRaises(RuntimeError) as ctx,
        ):
            get_cache("challenge", require_redis=True)
        self.assertIn("redis package is not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
