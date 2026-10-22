"""Persist unfilled maker quote definitions without fabricating executions."""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0031_paper_maker_intents"
down_revision = "20261022_0030_follower_experiments"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("paper_maker_intents"):
        return
    op.create_table(
        "paper_maker_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "config_id", sa.Integer(), sa.ForeignKey("market_maker_configs.id"), nullable=False
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("quote_key", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("config_id", "quote_key", name="uq_paper_maker_quote"),
    )
    op.create_index("ix_paper_maker_intents_config_id", "paper_maker_intents", ["config_id"])


def downgrade():
    op.drop_index("ix_paper_maker_intents_config_id", table_name="paper_maker_intents")
    op.drop_table("paper_maker_intents")
