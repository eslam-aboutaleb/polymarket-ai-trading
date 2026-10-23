"""Durable account-scoped modeled journal and NAV, separate from legacy history."""

import sqlalchemy as sa
from alembic import op

revision = "20261023_0033_paper_accounts"
down_revision = "20261023_0032_native_replay_capture"
branch_labels = None
depends_on = None


def upgrade():
    # Bootstrap installations can already contain current model metadata.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("paper_accounts"):
        op.create_table(
            "paper_accounts",
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("funding", sa.Numeric(28, 8), nullable=False),
            sa.Column("fee_bps", sa.Numeric(13, 8)),
            sa.Column("legacy_unverified", sa.Boolean(), nullable=False),
            sa.Column("legacy_acknowledged_at", sa.DateTime(timezone=True)),
            sa.Column("policy_json", sa.Text(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not inspector.has_table("paper_journal_events"):
        op.create_table(
            "paper_journal_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("event_id", sa.String(200), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("account_id", "event_id", name="uq_paper_journal_event"),
        )
    if not inspector.has_table("paper_order_records"):
        op.create_table(
            "paper_order_records",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("intent_key", sa.String(200), nullable=False),
            sa.Column("journal_sequence", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("account_id", "intent_key", name="uq_paper_order_intent"),
        )
    if not inspector.has_table("paper_nav_points"):
        op.create_table(
            "paper_nav_points",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("bucket_hour", sa.DateTime(timezone=True), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("valuation_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("account_id", "bucket_hour", name="uq_paper_nav_account_bucket"),
        )
        op.create_index(
            "ix_paper_nav_account_clock", "paper_nav_points", ["account_id", "captured_at"]
        )


def downgrade():
    for table in (
        "paper_nav_points",
        "paper_order_records",
        "paper_journal_events",
        "paper_accounts",
    ):
        op.drop_table(table)
