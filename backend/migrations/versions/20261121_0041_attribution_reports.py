"""Write-once attribution artifacts; financial balances are unaffected."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0041_attribution"
down_revision = "20261121_0040_live_balances"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("live_attribution_reports"):
        return
    op.create_table(
        "live_attribution_reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_json", sa.Text, nullable=False),
    )
    for name in ("input_hash", "user_id", "account_id"):
        op.create_index(f"ix_live_attribution_reports_{name}", "live_attribution_reports", [name])


def downgrade():
    op.drop_table("live_attribution_reports")
