"""Add execution-analytics columns to user_trades.

Adds expected/filled price & size, fee, slippage, latency and
strategy source so execution quality (slippage, fee drag, fill
rate) and the per-strategy Edge Score can be measured (plan 03).

Chains after plan 07's whale-monitoring migration (20261003_0004)
to keep a single linear alembic head:
..._0004 → _0005 (this) → _0006 → _0007.

Every DDL statement is guarded with an sa.inspect check: earlier
migrations in the chain run Base.metadata.create_all(checkfirst=True)
against the CURRENT models, so a fresh database may already contain
these columns and an unguarded op.add_column would fail with
"duplicate column name".

Revision ID: 20261003_0005
Revises: 20261003_0004
Create Date: 2026-10-03 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0005"
down_revision = "20261003_0004"
branch_labels = None
depends_on = None

# (column_name, type) pairs added to user_trades.
_EXECUTION_ANALYTICS_COLUMNS = [
    ("expected_price", sa.Float()),
    ("expected_size", sa.Float()),
    ("filled_price", sa.Float()),
    ("filled_size", sa.Float()),
    ("fee_paid", sa.Float()),
    ("slippage_bps", sa.Float()),
    ("latency_ms", sa.Float()),
    ("strategy_source", sa.String(length=50)),
]


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _existing_columns("user_trades")
    for name, col_type in _EXECUTION_ANALYTICS_COLUMNS:
        if name not in existing:
            op.add_column("user_trades", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    existing = _existing_columns("user_trades")
    for name, _ in reversed(_EXECUTION_ANALYTICS_COLUMNS):
        if name in existing:
            op.drop_column("user_trades", name)
