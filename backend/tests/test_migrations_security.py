import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _run_alembic(
    db_url: str, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": ".",
            "DATABASE_URL": db_url,
            "JWT_SECRET_KEY": "a" * 32,
            "REFRESH_TOKEN_HASH_SECRET": "b" * 32,
            "ENVIRONMENT": "test",
        }
    )
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class MigrationSecurityTests(unittest.TestCase):
    def test_upgrade_head_on_fresh_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fresh.db"
            result = _run_alembic(f"sqlite:///{db_path}", "upgrade", "head")
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"Alembic failed on fresh DB:\nSTDOUT:\n{result.stdout}"
                    f"\nSTDERR:\n{result.stderr}"
                ),
            )

    def test_refresh_token_legacy_backfill_path(self):
        """A pre-hardening database must have its refresh tokens hashed on upgrade.

        The fixture first migrates to ``20260228_0001`` — the revision immediately
        before the security-hardening migration that introduces token hashing —
        then rewrites the ``refresh_tokens`` table into its pre-hardening
        (plaintext) shape. That reproduces a real database written by an older
        release, without hand-rolling a schema that never actually existed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            db_url = f"sqlite:///{db_path}"

            # Create the schema as the pre-hardening release left it.
            result = _run_alembic(db_url, "upgrade", "20260228_0001")
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"Alembic failed staging the legacy schema:\nSTDOUT:\n{result.stdout}"
                    f"\nSTDERR:\n{result.stderr}"
                ),
            )

            conn = sqlite3.connect(db_path)
            try:
                # Recreate refresh_tokens in its plaintext shape.
                conn.execute("DROP TABLE refresh_tokens")
                conn.execute(
                    """
                    CREATE TABLE refresh_tokens (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        token VARCHAR NOT NULL UNIQUE,
                        expires_at DATETIME NOT NULL,
                        is_revoked BOOLEAN,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO refresh_tokens
                        (id, user_id, token, expires_at, is_revoked, created_at)
                    VALUES
                        (1, 99, 'legacy-refresh-token', '2026-12-01T00:00:00',
                         0, '2026-01-01T00:00:00')
                    """
                )
                conn.commit()
            finally:
                conn.close()

            result = _run_alembic(db_url, "upgrade", "head")
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"Alembic failed on legacy DB:\nSTDOUT:\n{result.stdout}"
                    f"\nSTDERR:\n{result.stderr}"
                ),
            )

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT token_hash, token FROM refresh_tokens WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

            self.assertIsNotNone(row)
            token_hash, token = row
            self.assertIsNotNone(token_hash)
            self.assertEqual(len(token_hash), 64)
            self.assertIsNone(token)


if __name__ == "__main__":
    unittest.main()
