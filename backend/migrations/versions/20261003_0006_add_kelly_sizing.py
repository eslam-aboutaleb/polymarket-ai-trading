"""Kelly sizing: user_settings.kelly_fraction.

Fractional-Kelly multiplier (0-1, default 0.25 = quarter
Kelly) for the new "kelly" copy-trade sizing mode and the
manual size-suggestion endpoint.  The range is enforced by
a check constraint on fresh databases and by the API
schema (ge=0, le=1) on every write path.

Revision ID: 20261003_0006
Revises: 20261003_0005
Create Date: 2026-10-03 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0006"
down_revision = "20261003_0005"
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
    if "kelly_fraction" not in columns:
        op.add_column(
            "user_settings",
            sa.Column(
                "kelly_fraction",
                sa.Float(),
                nullable=False,
                server_default="0.25",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_settings" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("user_settings")}
    if "kelly_fraction" in columns:
        op.drop_column("user_settings", "kelly_fraction")
