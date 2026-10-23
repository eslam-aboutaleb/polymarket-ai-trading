"""Persist bounded public venue evidence for frozen follower replays."""

import sqlalchemy as sa
from alembic import op

revision = "20261023_0032_native_replay_capture"
down_revision = "20261022_0031_paper_maker_intents"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("replay_captures"):
        return  # Legacy bootstrap already creates current model metadata.
    op.create_table(
        "replay_captures",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("condition_id", sa.String(66)),
        sa.Column("token_id", sa.String(200)),
        sa.Column("wallet", sa.String(42)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_at", sa.DateTime(timezone=True)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("rule_hash", sa.String(64)),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_replay_capture_token_clock", "replay_captures", ["token_id", "observed_at"])
    op.create_index("ix_replay_capture_wallet_clock", "replay_captures", ["wallet", "observed_at"])


def downgrade():
    op.drop_index("ix_replay_capture_wallet_clock", table_name="replay_captures")
    op.drop_index("ix_replay_capture_token_clock", table_name="replay_captures")
    op.drop_table("replay_captures")
