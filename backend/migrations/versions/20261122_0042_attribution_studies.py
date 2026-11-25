"""Prospective evaluation policies, separate from funds and order authorization."""

import sqlalchemy as sa
from alembic import op

revision = "20261122_0042_studies"
down_revision = "20261121_0041_attribution"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("attribution_studies"):
        return
    op.create_table(
        "attribution_studies",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_json", sa.Text, nullable=False),
    )
    for name in ("user_id", "account_id"):
        op.create_index(f"ix_attribution_studies_{name}", "attribution_studies", [name])


def downgrade():
    op.drop_table("attribution_studies")
