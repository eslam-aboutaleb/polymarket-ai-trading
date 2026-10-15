"""Process-role config tests (plan 10 Phase A).

The shipped ``docker-compose.prod.yml`` pins ``WEB_CONCURRENCY=1``
because a second web worker also starts every background scheduler,
which duplicates stop-loss orders. The ``ROLE`` setting is what lets
the HTTP tier scale safely, so its contract is pinned here:

* only the three documented roles are accepted,
* ``web`` owns no scheduler, ``worker`` serves no HTTP, ``all`` is both,
* an unknown role fails at import (settings validation), never at
  runtime mid-startup.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import ProcessRole, Settings


def _settings(**overrides) -> Settings:
    base = {
        "jwt_secret_key": "a" * 32,
        "refresh_token_hash_secret": "b" * 32,
        "environment": "test",
    }
    base.update(overrides)
    return Settings(**base)


class RoleTest(unittest.TestCase):
    def test_default_role_is_all(self):
        self.assertEqual(_settings().role, ProcessRole.ALL.value)

    def test_runs_background_schedulers_for_all_and_worker_only(self):
        self.assertTrue(_settings(role="all").runs_background_schedulers())
        self.assertTrue(_settings(role="worker").runs_background_schedulers())
        self.assertFalse(_settings(role="web").runs_background_schedulers())

    def test_serves_http_for_all_and_web_only(self):
        self.assertTrue(_settings(role="all").serves_http())
        self.assertTrue(_settings(role="web").serves_http())
        self.assertFalse(_settings(role="worker").serves_http())

    def test_role_is_case_insensitive(self):
        self.assertTrue(_settings(role="WORKER").runs_background_schedulers())

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            _settings(role="sideways")


class LifespanRoleTest(unittest.TestCase):
    """The lifespan honours the role: no monitor is started by ``web``."""

    def test_web_role_starts_no_background_service(self):
        import app.main as main_module

        async def _run():
            called: list[str] = []

            async def _fake_start(self=None):
                called.append("background")

            with (
                patch.object(main_module.settings, "role", "web"),
                patch.object(
                    main_module,
                    "_validate_startup_security",
                    lambda: None,
                ),
                patch.object(
                    main_module,
                    "_validate_database_connectivity",
                    lambda: None,
                ),
                patch.object(
                    main_module,
                    "_sync_admin_wallets",
                    lambda: None,
                ),
                patch.object(main_module, "_start_background_services", _fake_start),
            ):
                async with main_module.lifespan(object()):
                    pass
            return called

        import asyncio

        self.assertEqual(asyncio.run(_run()), [])

    def test_all_role_starts_background_services(self):
        import app.main as main_module

        async def _run():
            called: list[str] = []

            async def _fake_start(self=None):
                called.append("background")

            with (
                patch.object(main_module.settings, "role", "all"),
                patch.object(main_module, "_validate_startup_security", lambda: None),
                patch.object(main_module, "_validate_database_connectivity", lambda: None),
                patch.object(main_module, "_sync_admin_wallets", lambda: None),
                patch.object(main_module, "_start_background_services", _fake_start),
            ):
                async with main_module.lifespan(object()):
                    pass
            return called

        import asyncio

        self.assertEqual(asyncio.run(_run()), ["background"])


if __name__ == "__main__":
    unittest.main()
