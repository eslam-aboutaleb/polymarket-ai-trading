"""Frozen advisory portfolio allocation simulations (additive)."""

import sqlalchemy as sa
from alembic import op

# Distinct from the 0043 arbitrage migration on another branch. Both descend from 0042, so a
# merge of both branches needs `alembic merge` (two heads) or a down_revision rebase.
revision = "20261125_0044_portfolio_scenarios"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("portfolio_scenarios"):
        return
    op.create_table(
        "portfolio_scenarios",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("input_json", sa.Text, nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("proposal_json", sa.Text, nullable=False),
        sa.Column("allocator_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_portfolio_scenarios_owner", "portfolio_scenarios", ["user_id", "account_id"]
    )


def downgrade():
    op.drop_index("ix_portfolio_scenarios_owner", table_name="portfolio_scenarios")
    op.drop_table("portfolio_scenarios")
