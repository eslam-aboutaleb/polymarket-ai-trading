"""Frozen arbitrage feasibility runs (issue #9).

Revision id is deliberately distinct from other open 0043 migrations; whichever
lands second needs ``alembic merge`` or a ``down_revision`` rebase.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0043_arbitrage_feasibility"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("arbitrage_feasibility_runs"):
        return
    op.create_table(
        "arbitrage_feasibility_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column(
            "research_run_id", sa.String(36), sa.ForeignKey("book_research_runs.id"), nullable=False
        ),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("input_json", sa.Text, nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("result_json", sa.Text, nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_arb_feasibility_owner", "arbitrage_feasibility_runs", ["user_id", "account_id"]
    )


def downgrade():
    # Dropping only this table leaves the underlying book research evidence and
    # historical scans intact.
    op.drop_table("arbitrage_feasibility_runs")
