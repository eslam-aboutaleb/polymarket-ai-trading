"""Shared SlowAPI rate limiter instance.

The limiter lives here rather than in ``app.main`` so route modules can
apply per-endpoint limits without a circular import: ``app.main`` imports
the route modules, and the route modules need the limiter. ``app.main``
still owns the wiring (``app.state.limiter``, the ``RateLimitExceeded``
handler and the middleware).

Storage selection — per-worker behaviour of ``memory://``
---------------------------------------------------------
``REDIS_URL`` picks the counter backend:

* **set** — every worker process shares one counter set, so a deployment
  running more than one uvicorn worker (``docker-compose.prod.yml`` sets
  ``WEB_CONCURRENCY: ${WEB_CONCURRENCY:-2}`` with ``ROLE: web``) really
  enforces the configured budget: N concurrent requests from one client
  all land on the same counter.
* **unset** — SlowAPI falls back to ``memory://`` and **each worker
  process keeps its own counters**. The effective budget is therefore
  ``limit × worker_count``, not ``limit``: a client whose requests are
  spread over several workers gets that multiple of the budget through.
  That is fine (and normal) for a single-worker local stack, which is
  exactly where a Redis instance is usually absent, so the limiter warns
  rather than raising when it degrades to per-process storage in a
  non-local environment, and startup keeps working.

The environment is read straight from ``os.environ`` first, falling back
to the ``environment`` setting only when the variable is absent. That
keeps limiter construction independent of the settings cache and of
``app.config`` import order (this module is imported *by* the route
modules, which are imported *by* ``app.main``), while still honouring a
deployment that configures ``ENVIRONMENT`` only through ``.env`` /
compose. See ``_LOCAL_ENVIRONMENTS`` for the values treated as "a single
worker is fine".

The default budget follows the ``RATE_LIMIT_PER_MINUTE`` setting (100 by
default); environments that legitimately issue more traffic — for example
a local stack driven by an automated test suite behind one NAT — can
raise it.
"""

import logging
import os
from collections.abc import Mapping

from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)

# Environments that legitimately serve a single worker with no shared
# cache, where per-process (in-memory) counting is harmless. Mirrors
# ``Settings.is_local_environment`` without importing ``app.config``.
_LOCAL_ENVIRONMENTS = frozenset({"dev", "development", "local", "test"})


def _settings_environment() -> str:
    """Best-effort ``ENVIRONMENT`` from settings, for when the var is unset.

    Imported lazily and fully guarded so a settings/validation failure can
    never take the limiter (and therefore the whole app) down with it.
    """
    try:
        from app.config import get_settings

        return (get_settings().environment or "").strip().lower()
    except Exception:  # noqa: BLE001 - settings must never break limiter setup
        return ""


def _resolve_environment(environ: Mapping[str, str] | None = None) -> str:
    """Return the normalised environment name, lower-cased."""
    env = os.environ if environ is None else environ

    from_variable = (env.get("ENVIRONMENT") or "").strip()
    if from_variable:
        return from_variable.lower()
    return _settings_environment()


def _resolve_storage_uri(
    environ: Mapping[str, str] | None = None,
    environment: str | None = None,
) -> str:
    """Return the SlowAPI storage URI, warning when it degrades to memory.

    ``REDIS_URL`` wins when set. Otherwise the storage is ``memory://``
    (so startup never breaks — the limiter degrades to per-process
    counting) and a warning is logged whenever the environment is not
    one of the local/dev/test ones, because that is the configuration
    where "one counter set per worker" silently under-enforces the
    budget.

    Both arguments are injectable so callers (and tests) can exercise the
    decision without re-importing this module, which would rebuild the
    ``Limiter`` and drop every counter the rest of the process holds.
    """
    env = os.environ if environ is None else environ

    redis_url = (env.get("REDIS_URL") or "").strip()
    if redis_url:
        return redis_url

    resolved = _resolve_environment(env) if environment is None else environment.strip().lower()
    if resolved not in _LOCAL_ENVIRONMENTS:
        logger.warning(
            "REDIS_URL is not set, so the rate limiter is using per-process "
            "in-memory storage: limits are enforced separately in each worker "
            "process and a multi-worker deployment gets (limit x workers) "
            "effective budget. Set REDIS_URL to a shared Redis instance to "
            "enforce limits globally."
        )
    return "memory://"


storage_uri = _resolve_storage_uri()

try:  # pragma: no cover - defensive: settings must never break limiter setup
    from app.config import get_settings

    _default_limit = f"{max(1, int(get_settings().rate_limit_per_minute))}/minute"
except Exception:  # noqa: BLE001 - fall back to the historical default
    _default_limit = "100/minute"

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[_default_limit],
    storage_uri=storage_uri,
)
