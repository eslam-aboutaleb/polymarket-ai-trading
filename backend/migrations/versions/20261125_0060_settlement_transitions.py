"""Append-only settlement transition ledger (#24).

Distinct revision id: other branches also add their own 0043+ revisions (for
example redemption intents on the #56 branch). This revision follows head 0042;
if both land, add an Alembic merge revision.
"""

import sqlalchemy as sa
from alembic import op

from app.models.analytics.settlement_transition import immutability_ddl

revision = "20261125_0060_settlement_ledger"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("settlement_transitions"):
        return
    op.create_table(
        "settlement_transitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("condition_id", sa.String(66), nullable=False),
        sa.Column("token_id", sa.String(200), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("previous_state", sa.String(24)),
        sa.Column("evidence_class", sa.String(24), nullable=False),
        sa.Column("rule_hash", sa.String(64)),
        sa.Column("evidence_json", sa.Text, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_hash", sa.String(64)),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "account_id", "condition_id", "token_id", "sequence", name="uq_settlement_sequence"
        ),
        sa.UniqueConstraint(
            "account_id", "condition_id", "token_id", "prev_hash", name="uq_settlement_prev_hash"
        ),
    )
    op.create_index("ix_settlement_transitions_user_id", "settlement_transitions", ["user_id"])
    op.create_index("ix_settlement_transitions_account_id", "settlement_transitions", ["account_id"])
    op.create_index(
        "ix_settlement_transitions_condition",
        "settlement_transitions",
        ["account_id", "condition_id"],
    )
    for statement in immutability_ddl(bind.dialect.name):
        op.execute(statement)


def downgrade():
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT count(*) FROM settlement_transitions")).scalar()
    if count:
        raise RuntimeError("Refusing destructive downgrade: settlement evidence exists")
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS settlement_transitions_no_change ON settlement_transitions"
        )
        op.execute("DROP FUNCTION IF EXISTS settlement_transitions_immutable()")
    op.drop_table("settlement_transitions")
