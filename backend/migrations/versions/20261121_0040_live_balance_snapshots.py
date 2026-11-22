"""Append-only live balance evidence; no financial-history backfill."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0040_live_balances"
down_revision = "20261113_0039_resolution_rules"
branch_labels = None
depends_on = None


def upgrade():
    # The historical bootstrap also creates tables from current model metadata.
    if sa.inspect(op.get_bind()).has_table("live_balance_snapshots"):
        return
    op.create_table(
        "live_balance_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_json", sa.Text, nullable=False),
    )
    for name in ("user_id", "account_id", "observed_at"):
        op.create_index(f"ix_live_balance_snapshots_{name}", "live_balance_snapshots", [name])


def downgrade():
    op.drop_table("live_balance_snapshots")
