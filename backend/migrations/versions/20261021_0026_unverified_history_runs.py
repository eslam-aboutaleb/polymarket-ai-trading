"""Mark earlier backtest runs as unverified historical sources.

Runs created before the CLOB price-history adapter existed were computed
from unverified inputs, but nothing in the stored run said so. This
migration backfills ``indicator_values.history_quality`` for every run that
carries indicator values but no ``history_source``, so those runs stay
visible while remaining distinguishable from validated runs.

The marker is deliberately different from the ``unverified_fixture`` marker
the engine writes for fixture runs: a migrated legacy run and a fixture run
are different kinds of unverified evidence.

Revision ID: 20261021_0026_unverified_history_runs
Revises: 20261020_0022_trading_controls
"""

import sqlalchemy as sa
from alembic import op

revision = "20261021_0026_unverified_history_runs"
down_revision = "20261020_0022_trading_controls"
branch_labels = None
depends_on = None

UNVERIFIED_MARKER = "unverified_historical_source"


def _runs_table(bind):
    """Reflect the run table with an explicit JSON column type."""
    inspector = sa.inspect(bind)
    if "backtest_runs" not in inspector.get_table_names():
        return None
    return sa.Table(
        "backtest_runs",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("indicator_values", sa.JSON),
        autoload_with=bind,
    )


def upgrade() -> None:
    bind = op.get_bind()
    runs = _runs_table(bind)
    if runs is None:
        return
    rows = bind.execute(sa.select(runs.c.id, runs.c.indicator_values)).fetchall()
    for row in rows:
        values = row.indicator_values
        # Only indicator runs can carry a history source. A run without
        # indicator values never had a historical feed to verify.
        if not isinstance(values, dict):
            continue
        if values.get("history_source"):
            continue
        updated = {**values, "history_quality": UNVERIFIED_MARKER}
        bind.execute(runs.update().where(runs.c.id == row.id).values(indicator_values=updated))


def downgrade() -> None:
    # The marker only records that a legacy run has no verified source; it can
    # be removed without touching any other recorded evidence.
    bind = op.get_bind()
    runs = _runs_table(bind)
    if runs is None:
        return
    rows = bind.execute(sa.select(runs.c.id, runs.c.indicator_values)).fetchall()
    for row in rows:
        values = row.indicator_values
        if not isinstance(values, dict) or values.get("history_quality") != UNVERIFIED_MARKER:
            continue
        updated = {key: value for key, value in values.items() if key != "history_quality"}
        bind.execute(runs.update().where(runs.c.id == row.id).values(indicator_values=updated))
