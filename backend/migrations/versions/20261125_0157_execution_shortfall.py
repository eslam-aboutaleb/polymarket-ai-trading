"""Per-order implementation shortfall and fill-rate metrics (issue #157)."""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0157_exec_shortfall"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("execution_shortfalls"):
        return
    num = sa.Numeric(38, 18)
    op.create_table(
        "execution_shortfalls",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, nullable=True),
        sa.Column("strategy", sa.String(40), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("decision_id", sa.String(120), nullable=False),
        sa.Column("token_id", sa.String(200), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("shares_requested", num, nullable=False),
        sa.Column("shares_filled", num, nullable=False),
        sa.Column("maker_shares", num, nullable=False),
        sa.Column("decision_price", num, nullable=False),
        sa.Column("avg_fill_price", num, nullable=True),
        sa.Column("fees_usdc", num, nullable=False),
        sa.Column("shortfall_usdc", num, nullable=True),
        sa.Column("missed_shares", num, nullable=False),
        sa.Column("missed_cost_usdc", num, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "strategy", "decision_id", name="uq_exec_shortfall_decision"),
    )
    op.create_index("ix_execution_shortfalls_user_id", "execution_shortfalls", ["user_id"])
    op.create_index("ix_execution_shortfalls_account_id", "execution_shortfalls", ["account_id"])


def downgrade():
    op.drop_table("execution_shortfalls")
