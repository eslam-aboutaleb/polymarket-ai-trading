"""The history-source backfill marks earlier backtest runs as unverified.

Runs written before the CLOB price-history adapter existed were computed from
unverified inputs. The migration backfills ``indicator_values.history_quality``
for every run that carries indicator values but no ``history_source``, so those
runs stay visible while remaining distinguishable from validated runs.

The test stages the previous revision, inserts the legacy rows, upgrades to
head and reads the stored indicator values back.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from alembic.script import ScriptDirectory

BACKEND_DIR = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "20261020_0022_trading_controls"
HISTORY_REVISION = "20261021_0026_unverified_history_runs"
UNVERIFIED_MARKER = "unverified_historical_source"

INSERT_RUN = """
INSERT INTO backtest_runs (
    user_id, strategy_type, strategy_name, start_date, end_date, parameters,
    total_trades, winning_trades, losing_trades, total_pnl, status,
    indicator_values, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _run_alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
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
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _insert_run(conn: sqlite3.Connection, name: str, indicator_values: dict) -> None:
    conn.execute(
        INSERT_RUN,
        (
            1,
            "indicator",
            name,
            "2024-01-01",
            "2024-01-31",
            "{}",
            3,
            2,
            1,
            1.5,
            "completed",
            json.dumps(indicator_values),
            "2024-02-01T00:00:00",
        ),
    )


class UnverifiedHistoryMigrationTests(unittest.TestCase):
    def test_legacy_runs_are_marked_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "history.db"
            db_url = f"sqlite:///{db_path}"

            staged = _run_alembic(db_url, "upgrade", PREVIOUS_REVISION)
            self.assertEqual(staged.returncode, 0, msg=f"STDERR:\n{staged.stderr}")

            conn = sqlite3.connect(db_path)
            try:
                _insert_run(conn, "legacy", {"rsi": {"latest": 50}})
                _insert_run(
                    conn,
                    "validated",
                    {
                        "history_source": {"token_id": "123", "data_hash": "abc"},
                        "history_quality": "indicative_price_only",
                    },
                )
                _insert_run(conn, "fixture", {"history_quality": "unverified_fixture"})
                conn.commit()
            finally:
                conn.close()

            upgraded = _run_alembic(db_url, "upgrade", "head")
            self.assertEqual(upgraded.returncode, 0, msg=f"STDERR:\n{upgraded.stderr}")

            conn = sqlite3.connect(db_path)
            try:
                rows = dict(
                    conn.execute(
                        "SELECT strategy_name, indicator_values FROM backtest_runs"
                    ).fetchall()
                )
            finally:
                conn.close()

            legacy = json.loads(rows["legacy"])
            self.assertEqual(legacy["history_quality"], UNVERIFIED_MARKER)
            # The migration only adds the marker; it never drops recorded evidence.
            self.assertEqual(legacy["rsi"], {"latest": 50})

            validated = json.loads(rows["validated"])
            self.assertEqual(validated["history_quality"], "indicative_price_only")
            self.assertEqual(validated["history_source"]["token_id"], "123")

            fixture = json.loads(rows["fixture"])
            self.assertEqual(fixture["history_quality"], UNVERIFIED_MARKER)
            # The migrated marker must not be confused with the fixture marker.
            self.assertNotEqual(fixture["history_quality"], "unverified_fixture")

    def test_history_migration_remains_reachable_from_the_single_head(self):
        graph = ScriptDirectory(str(BACKEND_DIR / "migrations"))
        heads = graph.get_heads()
        self.assertEqual(len(heads), 1)
        # Later migrations may follow this backfill, including the shared
        # merge revision. Require reachability rather than freezing its name
        # as the tip of the graph forever.
        ancestors = {revision.revision for revision in graph.iterate_revisions(heads[0], "base")}
        self.assertIn(HISTORY_REVISION, ancestors)


if __name__ == "__main__":
    unittest.main()
