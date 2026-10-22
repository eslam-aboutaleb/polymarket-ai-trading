"""Capture immutable public market context for point-in-time research."""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0029_market_observations"
down_revision = "20261021_0028_quarantine_legacy_scores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical bootstrap migrations create the current model metadata, so a
    # fresh database already has this table and its index before reaching us.
    if sa.inspect(op.get_bind()).has_table("market_observations"):
        return
    op.create_table(
        "market_observations",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("condition_id", sa.String(66), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_market_observations_condition_observed",
        "market_observations",
        ["condition_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_observations_condition_observed", table_name="market_observations")
    op.drop_table("market_observations")
