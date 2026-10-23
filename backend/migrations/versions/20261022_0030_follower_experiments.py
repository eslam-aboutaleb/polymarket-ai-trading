"""Archive account-scoped follower research experiments."""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0030_follower_experiments"
down_revision = "20261022_0029_market_observations"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("follower_experiment_runs"):
        return
    op.create_table(
        "follower_experiment_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_follower_runs_owner_account", "follower_experiment_runs", ["user_id", "account_id"]
    )


def downgrade():
    op.drop_index("ix_follower_runs_owner_account", table_name="follower_experiment_runs")
    op.drop_table("follower_experiment_runs")
