"""
Challenge caching for authentication.
Supports in-memory cache (dev) and Redis (production).
"""

import json
import logging
import os
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

# Try to import redis, but make it optional
try:
    import redis

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)

# Try to import orjson, but keep stdlib fallback.
try:
    import orjson as _orjson

    ORJSON_AVAILABLE = True
except ImportError:
    _orjson = None
    ORJSON_AVAILABLE = False


def _json_dumps(value: Any) -> str:
    if ORJSON_AVAILABLE and _orjson is not None:
        return _orjson.dumps(value).decode("utf-8")
    return json.dumps(value)


def _json_loads(data: str) -> Any:
    if ORJSON_AVAILABLE and _orjson is not None:
        return _orjson.loads(data)
    return json.loads(data)


def _is_local_environment() -> bool:
    env = os.environ.get("ENVIRONMENT", "development").strip().lower()
    return env in {"dev", "development", "local", "test"}


def _bool_env(var_name: str, default: bool) -> bool:
    raw = os.environ.get(var_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class InMemoryCache:
    """Simple in-memory cache for development/testing."""

    def __init__(self, namespace: str, max_size: int | None = None):
        self._namespace = namespace
        if max_size is None:
            try:
                from app.config import get_settings

                max_size = get_settings().in_memory_cache_max_entries
            except Exception:
                max_size = 10000
        self._max_size = max(1, int(max_size))
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    @staticmethod
    def _now_ts() -> float:
        return datetime.now(UTC).timestamp()

    def _evict_expired(self) -> None:
        now_ts = self._now_ts()
        expired_keys = [
            key for key, entry in self._cache.items() if now_ts > float(entry["expires_at"])
        ]
        for key in expired_keys:
            self._cache.pop(key, None)

    def _enforce_max_size(self) -> None:
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        """Store value with TTL."""
        self._evict_expired()
        expires_at = self._now_ts() + ttl_seconds
        namespaced = self._key(key)
        self._cache[namespaced] = {
            "value": value,
            "expires_at": expires_at,
        }
        self._cache.move_to_end(namespaced)
        self._enforce_max_size()

    def get(self, key: str) -> Any | None:
        """Get value if exists and not expired."""
        namespaced = self._key(key)
        if namespaced not in self._cache:
            return None

        entry = self._cache[namespaced]
        if self._now_ts() > float(entry["expires_at"]):
            del self._cache[namespaced]
            return None

        # LRU touch on read.
        self._cache.move_to_end(namespaced)
        return entry["value"]

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        namespaced = self._key(key)
        if namespaced in self._cache:
            del self._cache[namespaced]
            return True
        return False

    def clear(self) -> None:
        """Clear all entries."""
        self._cache.clear()


class RedisCache:
    """Redis-based cache for production use."""

    def __init__(self, redis_url: str, namespace: str):
        if not REDIS_AVAILABLE:
            raise ImportError("redis package not installed. Run: pip install redis")
        self._client = redis.from_url(redis_url, decode_responses=True)
        self._prefix = f"polymarket:{namespace}:"

    def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        """Store value with TTL in Redis."""
        full_key = f"{self._prefix}{key}"
        self._client.setex(full_key, ttl_seconds, _json_dumps(value))

    def get(self, key: str) -> Any | None:
        """Get value from Redis."""
        full_key = f"{self._prefix}{key}"
        data = self._client.get(full_key)
        if data:
            return _json_loads(data)
        return None

    def delete(self, key: str) -> bool:
        """Delete key from Redis."""
        full_key = f"{self._prefix}{key}"
        return bool(self._client.delete(full_key))

    def clear(self) -> None:
        """Clear all challenge entries."""
        keys = self._client.keys(f"{self._prefix}*")
        if keys:
            self._client.delete(*keys)


def get_cache(namespace: str, *, require_redis: bool = False):
    """
    Factory function to get appropriate cache based on environment.
    Uses Redis if REDIS_URL is set, otherwise falls back to in-memory.
    """
    redis_url = os.environ.get("REDIS_URL")

    if redis_url and REDIS_AVAILABLE:
        try:
            cache = RedisCache(redis_url, namespace)
            # Test connection
            cache._client.ping()
            return cache
        except Exception as e:
            if require_redis:
                raise RuntimeError(
                    f"Redis connection failed for required cache namespace '{namespace}'."
                ) from e
            logger.warning(
                "Redis connection failed for namespace '%s'; falling back to in-memory cache.",
                namespace,
                exc_info=True,
            )
            return InMemoryCache(namespace)

    if require_redis:
        if not redis_url:
            raise RuntimeError(
                f"REDIS_URL must be configured for required cache namespace '{namespace}'."
            )
        if not REDIS_AVAILABLE:
            raise RuntimeError(
                f"redis package is not installed for required cache namespace '{namespace}'."
            )

    return InMemoryCache(namespace)


# Singleton cache instance for challenge storage. Challenges gate
# authentication, so with more than one uvicorn worker the cache
# must be shared: an in-process cache lets /login and /verify hit
# different processes and fail with "No pending challenge found".
# Mirror the credentials pattern and require Redis outside local
# environments.
try:
    from app.config import get_settings

    _cache_settings = get_settings()
    _require_redis_for_challenges = _cache_settings.require_redis_for_challenges
    _challenge_local_env = _cache_settings.is_local_environment
    _require_redis_for_credentials = _cache_settings.require_redis_for_credentials
    _credentials_local_env = _cache_settings.is_local_environment
except Exception:
    _require_redis_for_challenges = _bool_env("REQUIRE_REDIS_FOR_CHALLENGES", True)
    _challenge_local_env = _is_local_environment()
    _require_redis_for_credentials = _bool_env("REQUIRE_REDIS_FOR_CREDENTIALS", True)
    _credentials_local_env = _is_local_environment()

challenge_cache = get_cache(
    "challenge",
    require_redis=(not _challenge_local_env and _require_redis_for_challenges),
)

# Separate cache for storing private keys / CLOB API creds per wallet (long TTL)
# Keys expire after 30 days (CREDENTIAL_CACHE_TTL_SECONDS) or on logout
key_store = get_cache(
    "credentials",
    require_redis=(not _credentials_local_env and _require_redis_for_credentials),
)

# Cache for AI opportunity analysis results (20-minute TTL)
opportunity_cache = get_cache("opportunity")
