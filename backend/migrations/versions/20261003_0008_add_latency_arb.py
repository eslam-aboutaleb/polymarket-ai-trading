"""Latency arbitrage: latency_arb_configs table.

Per-user configuration for the plan 06 latency-arbitrage
engine: enabled flag, edge threshold, max notional, symbol
and window selection, late-entry toggle, per-strategy daily
loss limit and opportunity-alert toggle.

Revision ID: 20261003_0008
Revises: 20261003_0007
Create Date: 2026-10-03 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0008"
down_revision = "20261003_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Guarded: earlier migrations in this chain run
    # Base.metadata.create_all(checkfirst=True) against the
    # current models, so a fresh database may already have
    # this table by the time this migration runs (an
    # unguarded op.create_table would fail with
    # "table already exists").
    if "latency_arb_configs" in inspector.get_table_names():
        return

    op.create_table(
        "latency_arb_configs",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("edge_threshold", sa.Float(), nullable=False),
        sa.Column("max_notional", sa.Float(), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("windows", sa.JSON(), nullable=False),
        sa.Column("late_entry", sa.Boolean(), nullable=False),
        sa.Column("daily_loss_limit", sa.Float(), nullable=False),
        sa.Column("alert_on_opportunity", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "latency_arb_configs" in inspector.get_table_names():
        op.drop_table("latency_arb_configs")
