import importlib
import os
import unittest
from unittest.mock import patch

from app.config import get_settings


class DatabaseSecurityTests(unittest.TestCase):
    def setUp(self):
        import app.utils.database as database

        # Reloading this shared module rebinds its settings,
        # engine, session factory and get_db dependency — and
        # every other module (routes, services, app.main) holds
        # references to the originals. FastAPI dependency
        # overrides also match on function identity. Save and
        # restore the originals so later test files keep
        # working against the development engine.
        self._database = database
        self._saved = {
            name: getattr(database, name)
            for name in ("settings", "engine", "SessionLocal", "get_db")
        }
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            setattr(self._database, name, value)

    def test_production_engine_kwargs_enforce_tls_and_pooling(self):
        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "a" * 32,
                "ENVIRONMENT": "production",
                "DATABASE_URL": "postgresql://user:pass@localhost:5432/polymarket",
            },
            clear=False,
        ):
            get_settings.cache_clear()
            import app.utils.database as database

            database = importlib.reload(database)
            kwargs = database._database_engine_kwargs()

            self.assertFalse(kwargs["echo"])
            self.assertEqual(kwargs["connect_args"]["sslmode"], "require")
            self.assertEqual(kwargs["pool_size"], 10)
            self.assertEqual(kwargs["max_overflow"], 20)
            self.assertTrue(kwargs["pool_pre_ping"])

    def test_local_engine_kwargs_skip_tls_and_enable_echo(self):
        with patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "a" * 32,
                "ENVIRONMENT": "development",
                "DATABASE_URL": "postgresql://user:pass@localhost:5432/polymarket",
            },
            clear=False,
        ):
            get_settings.cache_clear()
            import app.utils.database as database

            database = importlib.reload(database)
            kwargs = database._database_engine_kwargs()

            self.assertTrue(kwargs["echo"])
            self.assertNotIn("connect_args", kwargs)


if __name__ == "__main__":
    unittest.main()
