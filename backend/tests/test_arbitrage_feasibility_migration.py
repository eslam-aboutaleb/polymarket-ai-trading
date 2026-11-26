"""Upgrade/rollback of the arbitrage feasibility table keeps earlier evidence."""

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REVISION = "20261125_0043_arbitrage_feasibility"
PARENT = "20261122_0042_studies"


def _alembic(db_url, *args):
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
    return subprocess.run(  # noqa: S603 - fixed argv, shell=False
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


def test_upgrade_then_rollback_preserves_research_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "arb.db"
        url = f"sqlite:///{path}"
        up = _alembic(url, "upgrade", REVISION)
        assert up.returncode == 0, up.stderr
        tables = _tables(path)
        assert "arbitrage_feasibility_runs" in tables and "book_research_runs" in tables
        down = _alembic(url, "downgrade", PARENT)
        assert down.returncode == 0, down.stderr
        tables = _tables(path)
        assert "arbitrage_feasibility_runs" not in tables
        assert "book_research_runs" in tables
        again = _alembic(url, "upgrade", REVISION)
        assert again.returncode == 0, again.stderr
        assert "arbitrage_feasibility_runs" in _tables(path)


def test_revision_chain_is_linear_from_parent():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision(REVISION)
    assert rev.down_revision == PARENT
