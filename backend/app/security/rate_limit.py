"""Shared SlowAPI rate limiter instance.

The limiter lives here rather than in ``app.main`` so route modules can
apply per-endpoint limits without a circular import: ``app.main`` imports
the route modules, and the route modules need the limiter. ``app.main``
still owns the wiring (``app.state.limiter``, the ``RateLimitExceeded``
handler and the middleware).

Storage defaults to an in-process memory backend when ``REDIS_URL`` is
unset, which is fine for a single worker; multi-worker deployments should
set ``REDIS_URL`` so limits are shared across processes.
"""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],
    storage_uri=os.environ.get("REDIS_URL", "memory://"),
)
