"""Shared pytest configuration for the backend test suite.

Settings validation runs at import time in several modules, so the required
secrets are seeded once here. ``setdefault`` is used deliberately: a test that
needs a specific value still overrides it via ``patch.dict(os.environ, ...)``,
and tests such as ``test_security_config.py`` that assert on invalid values
pass them explicitly to ``Settings(...)``.

Keeping this in one place stops an individual test module from failing to import
merely because it touches configuration first.
"""

import os

# Required by Settings.validate_jwt_secret_key: at least 32 characters and not
# a recognised placeholder. Any 32-character string satisfies the check.
os.environ.setdefault("JWT_SECRET_KEY", "a" * 32)
os.environ.setdefault("REFRESH_TOKEN_HASH_SECRET", "b" * 32)

# Settings refuses to start against production defaults implicitly; the test
# environment is explicitly non-production.
os.environ.setdefault("ENVIRONMENT", "test")
