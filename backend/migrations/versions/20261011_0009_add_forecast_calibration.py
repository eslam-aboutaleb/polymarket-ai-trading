"""Add forecast-calibration columns to assessments.

Adds the AI probability, the market price at assessment time, model /
provider / task attribution, and the resolution outcome + Brier score
so AI forecasts can be scored against how markets actually resolved
(plan 01).

Also relaxes assessments.trade_history_id to nullable: opportunity
scan verdicts have no source trade_history row, so calibration rows
can exist without one.

Chains after the latency-arb migration (20261003_0008) to keep a
single linear alembic head:
..._0008 → _0009 (this) → _0010 → _0011.

Every DDL statement is guarded with an sa.inspect check: earlier
migrations in the chain run Base.metadata.create_all(checkfirst=True)
against the CURRENT models, so a fresh database may already contain
these columns and an unguarded op.add_column would fail with
"duplicate column name".

Revision ID: 20261011_0009
Revises: 20261003_0008
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0009"
down_revision = "20261003_0008"
branch_labels = None
depends_on = None

# (column_name, type) pairs added to assessments.
_CALIBRATION_COLUMNS = [
    ("ai_probability", sa.Float()),
    ("market_price_at_assessment", sa.Float()),
    ("resolved_outcome", sa.String(length=20)),
    ("brier_score", sa.Float()),
    ("resolved_at", sa.DateTime(timezone=True)),
    ("model_name", sa.String(length=100)),
    ("provider", sa.String(length=50)),
    ("task", sa.String(length=50)),
    ("condition_id", sa.String(length=100)),
]


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _existing_columns("assessments")
    for name, col_type in _CALIBRATION_COLUMNS:
        if name not in existing:
            op.add_column("assessments", sa.Column(name, col_type, nullable=True))

    # SQLite cannot ALTER a column in place: batch mode rewrites the
    # table while preserving the foreign key.
    with op.batch_alter_table("assessments") as batch_op:
        batch_op.alter_column("trade_history_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # DATA LOSS: opportunity-scan verdicts (plan 01) store
    # ``trade_history_id = NULL`` by design, and the flip below re-imposes
    # NOT NULL on that column. Those calibration rows therefore cannot
    # survive the downgrade, so they are deleted first — without this DELETE
    # the batch ALTER fails with an IntegrityError on every database that has
    # scan verdicts, and on SQLite it also leaves a stray
    # ``_alembic_tmp_assessments`` table behind. Deleting them is what makes
    # the rollback deterministic; the trade-backed assessments are untouched
    # and their calibration columns are dropped afterwards.
    op.execute("DELETE FROM assessments WHERE trade_history_id IS NULL")

    with op.batch_alter_table("assessments") as batch_op:
        batch_op.alter_column("trade_history_id", existing_type=sa.Integer(), nullable=False)

    existing = _existing_columns("assessments")
    for name, _ in reversed(_CALIBRATION_COLUMNS):
        if name in existing:
            op.drop_column("assessments", name)
