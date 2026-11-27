"""Durable redemption identities; preserve legacy attempts without inferred provenance."""

import sqlalchemy as sa
from alembic import op

revision = "20261122_0043_redemption"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("redemption_intents"):
        op.create_table(
            "redemption_intents",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
            sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("idempotency_key", sa.String(160), nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("execution_mode", sa.String(12), nullable=False),
            sa.Column("chain_id", sa.Integer, nullable=False),
            sa.Column("wallet", sa.String(42), nullable=False),
            sa.Column("condition_id", sa.String(66), nullable=False),
            sa.Column("snapshot_json", sa.Text, nullable=False),
            sa.Column("snapshot_hash", sa.String(64), nullable=False),
            sa.Column("state", sa.String(24), nullable=False),
            sa.Column("nonce", sa.Integer),
            sa.Column("transaction_json", sa.Text),
            sa.Column("transaction_hash", sa.String(66), unique=True),
            sa.Column("receipt_json", sa.Text),
            sa.Column("receipt_hash", sa.String(64)),
            sa.Column("payout", sa.Numeric(36, 6)),
            sa.Column("finalized_once", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("reason", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("account_id", "idempotency_key", name="uq_redemption_intent_key"),
            sa.UniqueConstraint("chain_id", "wallet", "nonce", name="uq_redemption_wallet_nonce"),
        )
        for name in ("user_id", "account_id"):
            op.create_index(f"ix_redemption_intents_{name}", "redemption_intents", [name])
    if not sa.inspect(bind).has_table("redemption_wallet_leases"):
        op.create_table(
            "redemption_wallet_leases",
            sa.Column("chain_id", sa.Integer, primary_key=True),
            sa.Column("wallet", sa.String(42), primary_key=True),
            sa.Column("active_intent_id", sa.String(64), sa.ForeignKey("redemption_intents.id")),
        )
    if not sa.inspect(bind).has_table("redemption_observations"):
        op.create_table(
            "redemption_observations",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column(
                "intent_id", sa.String(64), sa.ForeignKey("redemption_intents.id"), nullable=False
            ),
            sa.Column("proof_hash", sa.String(64), nullable=False),
            sa.Column("proof_json", sa.Text, nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_redemption_observations_intent_id", "redemption_observations", ["intent_id"]
        )
    if "intent_id" not in {c["name"] for c in sa.inspect(bind).get_columns("redemption_attempts")}:
        with op.batch_alter_table("redemption_attempts") as batch:
            batch.add_column(sa.Column("intent_id", sa.String(64)))
            batch.create_foreign_key(
                "fk_redemption_attempt_intent", "redemption_intents", ["intent_id"], ["id"]
            )
            batch.create_unique_constraint("uq_redemption_attempt_intent", ["intent_id"])


def downgrade():
    # Rolling the application back is safe; deleting recorded signing identities
    # is not. Preserve populated evidence until an explicit archival migration.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM redemption_intents")).scalar():
        raise RuntimeError("Redemption intent evidence exists; preserve the schema during rollback")
    with op.batch_alter_table("redemption_attempts") as batch:
        batch.drop_constraint("uq_redemption_attempt_intent", type_="unique")
        batch.drop_constraint("fk_redemption_attempt_intent", type_="foreignkey")
        batch.drop_column("intent_id")
    op.drop_table("redemption_observations")
    op.drop_table("redemption_wallet_leases")
    op.drop_table("redemption_intents")
