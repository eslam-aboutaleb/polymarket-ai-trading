"""Add the daily_digest_enabled preference to user_settings.

Opt-out flag for the scheduled daily portfolio digest report
(plan 02). Defaults to enabled for existing rows via a server
default, matching the app-level default.

Chains after the portfolio-snapshots migration (20261011_0010) to
keep a single linear alembic head:
..._0010 → _0011 (this).

Revision ID: 20261011_0011
Revises: 20261011_0010
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0011"
down_revision = "20261011_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("user_settings")}
    if "daily_digest_enabled" not in existing:
        op.add_column(
            "user_settings",
            sa.Column(
                "daily_digest_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("user_settings")}
    if "daily_digest_enabled" in existing:
        op.drop_column("user_settings", "daily_digest_enabled")
