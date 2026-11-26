"""Account-owned execution estimates for implementation shortfall reconciliation."""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0043_execution_estimates"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("execution_estimates"):
        return
    op.create_table(
        "execution_estimates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("estimate_id", sa.String(64), nullable=False),
        sa.Column("order_intent_id", sa.String(80), nullable=False),
        sa.Column("condition_id", sa.String(200), nullable=False),
        sa.Column("token_id", sa.String(200), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("shares", sa.Numeric(24, 12), nullable=False),
        sa.Column("book_hash", sa.String(64), nullable=True),
        sa.Column("arrival_mid", sa.Numeric(24, 12), nullable=True),
        sa.Column("benchmark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimate_json", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_execution_estimates_owner",
        "execution_estimates",
        ["user_id", "account_id", "created_at"],
    )
    op.create_index("ix_execution_estimates_estimate", "execution_estimates", ["estimate_id"])


def downgrade():
    op.drop_table("execution_estimates")
