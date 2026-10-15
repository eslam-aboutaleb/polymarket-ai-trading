"""Add performance indexes for the consensus / calibration / drift reads (plan 07).

The consensus, calibration, model-drift and forecast-resolution paths all
scan ``assessments`` by ``condition_id`` and by ``created_at`` (windowed) and
filter ``resolved_at IS NULL``; ``calibration`` also joins on
``user_trades.source_trade_history_id``. None of those columns were indexed,
so each read degrades to a sequential scan as the tables grow.

Additive only (CREATE INDEX), guarded by ``sa.inspect`` so a re-run against a
database that already has an index is a no-op. Chains after
20261011_0019 to keep a single linear alembic head.

Revision ID: 20261015_0020_add_perf_indexes
Revises: 20261011_0019
Create Date: 2026-10-15 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261015_0020_add_perf_indexes"
down_revision = "20261011_0019"
branch_labels = None
depends_on = None

#: (index name, table, columns) — one index per hot filter/join column.
_INDEXES = (
    ("ix_assessments_condition_id", "assessments", ["condition_id"]),
    ("ix_assessments_created_at", "assessments", ["created_at"]),
    ("ix_assessments_resolved_at", "assessments", ["resolved_at"]),
    ("ix_user_trades_source_trade_history_id", "user_trades", ["source_trade_history_id"]),
    ("ix_user_trades_user_status", "user_trades", ["user_id", "status"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table, columns in _INDEXES:
        if table not in inspector.get_table_names():
            continue
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table, _columns in reversed(_INDEXES):
        if table not in inspector.get_table_names():
            continue
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name in existing:
            op.drop_index(name, table_name=table)
