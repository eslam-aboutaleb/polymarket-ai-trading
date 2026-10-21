"""The review migrations upgrade alone and from any previously applied PR head."""

from pathlib import Path
from shutil import copy2

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

BASE = "20261020_0022_trading_controls"
BRANCH_HEADS = (
    "20261021_0023_forecast_scoring_integrity",
    "20261021_0023",
    "20261021_0025_x_indexed_provenance",
    "20261021_0026_unverified_history_runs",
)
MERGE = "20261021_0027_merge_review_heads"

ENV = """
from alembic import context

with context.config.attributes['connection'].begin():
    context.configure(connection=context.config.attributes['connection'])
    context.run_migrations()
"""

BASE_MIGRATION = """
from alembic import op
import sqlalchemy as sa

revision = "20261020_0022_trading_controls"
down_revision = None

def upgrade():
    op.create_table("assessments", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("brier_score", sa.Float), sa.Column("resolved_outcome", sa.String),
                    sa.Column("resolved_at", sa.String))
    op.create_table("inverse_bot_positions", sa.Column("id", sa.Integer, primary_key=True))
    op.create_table("redemption_attempts", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("status", sa.String))
    op.create_table("backtest_runs", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("indicator_values", sa.JSON))

def downgrade():
    for table in ("backtest_runs", "redemption_attempts", "inverse_bot_positions", "assessments"):
        op.drop_table(table)
"""


@pytest.mark.parametrize("previous_head", (BASE, *BRANCH_HEADS))
def test_upgrade_from_each_independent_head_preserves_evidence(tmp_path, previous_head):
    """Exercise actual Alembic traversal/DDL, including already applied sibling heads."""
    scripts = tmp_path / "migrations"
    versions = scripts / "versions"
    versions.mkdir(parents=True)
    (scripts / "env.py").write_text(ENV)
    (versions / "baseline.py").write_text(BASE_MIGRATION)
    source = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    for migration in source.glob("20261021_*.py"):
        copy2(migration, versions / migration.name)

    config = Config()
    config.set_main_option("script_location", str(scripts))
    graph = ScriptDirectory.from_config(config)
    assert len(graph.get_heads()) == 1
    assert set(graph.get_revision(MERGE).down_revision) == set(BRANCH_HEADS)
    has_quarantine = (versions / "20261021_0028_quarantine_legacy_scores.py").exists()

    engine = sa.create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, BASE)
            connection.execute(
                sa.text(
                    "INSERT INTO assessments (id, brier_score, resolved_outcome, resolved_at) "
                    "VALUES (1, 0.04, 'YES', '2026-10-20T00:00:00Z')"
                )
            )
            connection.execute(
                sa.text("INSERT INTO redemption_attempts (id, status) VALUES (1, 'pending')")
            )
            connection.execute(
                sa.text("INSERT INTO backtest_runs (id, indicator_values) VALUES (1, '{}')")
            )
            connection.commit()
            command.upgrade(config, previous_head)
            command.upgrade(config, "head")
            inspector = sa.inspect(connection)
            assert "last_x_source_type" in {
                column["name"] for column in inspector.get_columns("inverse_bot_positions")
            }
            assert (
                connection.execute(
                    sa.text("SELECT status FROM redemption_attempts WHERE id = 1")
                ).scalar_one()
                == "legacy_unknown"
            )
            assessments = sa.Table("assessments", sa.MetaData(), autoload_with=connection)
            row = connection.execute(sa.select(assessments)).one()._mapping
            assert row["brier_score"] == 0.04
            assert row["resolved_outcome"] == "YES"
            assert row["resolved_at"] == "2026-10-20T00:00:00Z"
            assert row["scoring_version"] is None
            if has_quarantine:
                assert row["exclusion_reason"] == "legacy_unverified_resolution"
            runs = sa.Table("backtest_runs", sa.MetaData(), autoload_with=connection)
            assert connection.execute(sa.select(runs.c.indicator_values)).scalar_one() == {
                "history_quality": "unverified_historical_source"
            }
            connection.commit()
            # Repeat application, then a rollback and re-upgrade. Never invent
            # chain evidence or erase an original forecast score on either path.
            command.upgrade(config, "head")
            command.downgrade(config, BASE)
            command.upgrade(config, "head")
            assert (
                connection.execute(
                    sa.text("SELECT brier_score FROM assessments WHERE id = 1")
                ).scalar_one()
                == 0.04
            )
    finally:
        engine.dispose()


def test_complete_repository_has_one_migration_head():
    scripts = Path(__file__).resolve().parents[1] / "migrations"
    assert len(ScriptDirectory(str(scripts)).get_heads()) == 1
