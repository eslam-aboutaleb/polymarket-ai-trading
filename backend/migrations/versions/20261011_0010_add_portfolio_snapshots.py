"""Add the portfolio_snapshots table for the equity curve.

One row per (user, hour) capturing bankroll (USDC + unrealized PnL),
the invested/current position totals, and the position count so the
Dashboard can chart portfolio equity over time (plan 05).

Chains after the forecast-calibration migration (20261011_0009) to
keep a single linear alembic head:
..._0009 → _0010 (this) → _0011.

Revision ID: 20261011_0010
Revises: 20261011_0009
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0010"
down_revision = "20261011_0009"
branch_labels = None
depends_on = None

# Indexes this migration owns, in the order they must be dropped.
_INDEXES = (
    "uq_portfolio_snapshots_user_bucket",
    "ix_portfolio_snapshots_bucket_hour",
    "ix_portfolio_snapshots_user_id",
)


def _existing_indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "portfolio_snapshots" in inspector.get_table_names():
        return

    # Mirrors app/models/portfolio_snapshot.py exactly: the user_id FK and the
    # two ``index=True`` columns (user_id, bucket_hour) are part of the model,
    # so a database migrated here and one created by ``create_all`` must not
    # diverge — otherwise a downgrade/re-upgrade or a new deployment silently
    # runs different SQL for the same table.
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("bucket_hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usdc_balance", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("bankroll", sa.Float(), nullable=False),
        sa.Column("total_invested", sa.Float(), nullable=False),
        sa.Column("total_current_value", sa.Float(), nullable=False),
        sa.Column("positions_count", sa.Integer(), nullable=False),
        sa.Column("is_paper", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_portfolio_snapshots_user_id", "portfolio_snapshots", ["user_id"])
    op.create_index("ix_portfolio_snapshots_bucket_hour", "portfolio_snapshots", ["bucket_hour"])
    op.create_index(
        "uq_portfolio_snapshots_user_bucket",
        "portfolio_snapshots",
        ["user_id", "bucket_hour"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "portfolio_snapshots" not in inspector.get_table_names():
        return

    # Drop only the indexes that are actually there. The baseline migration
    # (20260228_0001) creates this table from the model, where the same
    # uniqueness comes from a ``UniqueConstraint`` (SQLite's
    # ``sqlite_autoindex_…``) rather than a named index — so on a
    # chain-built database the upgrade above was skipped and there is no
    # ``uq_…`` index to drop. An unconditional drop makes the downgrade fail,
    # which is how a rollback turns into a stuck alembic version.
    present = _existing_indexes("portfolio_snapshots")
    for name in _INDEXES:
        if name in present:
            op.drop_index(name, table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
