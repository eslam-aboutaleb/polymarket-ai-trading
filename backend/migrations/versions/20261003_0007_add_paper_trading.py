"""Paper trading: user_settings.paper_balance.

Paper account balance (USDC, default 1000.0) that paper
PnL is tracked against, separate from real equity.  The
column is non-negative and exposed via GET/PUT
/api/settings and GET /api/settings/paper-summary.

Revision ID: 20261003_0007
Revises: 20261003_0006
Create Date: 2026-10-03 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0007"
down_revision = "20261003_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Every statement is guarded: earlier migrations in
    # this chain run Base.metadata.create_all(checkfirst=True)
    # against the current models, so a fresh database may
    # already have this column by the time this migration
    # runs (an unguarded op.add_column would fail with
    # "duplicate column name").
    if "user_settings" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("user_settings")}
    if "paper_balance" not in columns:
        op.add_column(
            "user_settings",
            sa.Column(
                "paper_balance",
                sa.Float(),
                nullable=False,
                server_default="1000.0",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_settings" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("user_settings")}
    if "paper_balance" in columns:
        op.drop_column("user_settings", "paper_balance")
