"""Add forecast scoring-integrity columns to assessments.

Adds the three columns the forecast/resolution integrity work needs
(issue #29):

* ``scoring_version`` — the scoring rule that produced ``brier_score``,
  so a change of rule is distinguishable from the rows written under the
  previous one,
* ``exclusion_reason`` — why a forecast could not be scored (closed but
  not finalized, disputed, void, multi-outcome, ambiguous settlement,
  wrong market, unusable probability, forecast recorded after the close),
* ``probability_null_reason`` — why a verdict carries no probability.

Chains after the trading-controls migration (20261020_0022) to keep a
single linear alembic head.

Every DDL statement is guarded with an sa.inspect check: the baseline
migration runs Base.metadata.create_all against the CURRENT models, so a
fresh database may already contain these columns and an unguarded
op.add_column would fail with "duplicate column name".

Revision ID: 20261021_0023_forecast_scoring_integrity
Revises: 20261020_0022_trading_controls
Create Date: 2026-10-21 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261021_0023_forecast_scoring_integrity"
down_revision = "20261020_0022_trading_controls"
branch_labels = None
depends_on = None

# (column_name, type) pairs added to assessments.
_SCORING_COLUMNS = [
    ("scoring_version", sa.String(length=32)),
    ("exclusion_reason", sa.String(length=40)),
    ("probability_null_reason", sa.String(length=40)),
]


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _existing_columns("assessments")
    for name, col_type in _SCORING_COLUMNS:
        if name not in existing:
            op.add_column("assessments", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    existing = _existing_columns("assessments")
    for name, _ in reversed(_SCORING_COLUMNS):
        if name in existing:
            op.drop_column("assessments", name)
