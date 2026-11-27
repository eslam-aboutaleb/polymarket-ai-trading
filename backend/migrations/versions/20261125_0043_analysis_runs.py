"""Immutable structured Market Deep Dive run records."""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0043_analysis_runs"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("analysis_runs"):
        return
    op.create_table(
        "analysis_runs",
        sa.Column("analysis_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("condition_id", sa.String(66), nullable=False),
        sa.Column("selected_token_id", sa.String(120)),
        sa.Column("snapshot_id", sa.String(64)),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(16)),
        sa.Column("provider", sa.String(50)),
        sa.Column("model", sa.String(100)),
        sa.Column("forecast_id", sa.Integer, sa.ForeignKey("assessments.id")),
        sa.Column("forecast_p_yes", sa.String(40)),
        sa.Column("market_p_yes", sa.String(40)),
        sa.Column("evidence_refs_json", sa.Text, nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason_codes_json", sa.Text, nullable=False),
        sa.Column("completeness_json", sa.Text, nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("pivotal_claims", sa.Integer, nullable=False, server_default="0"),
        sa.Column("unsupported_pivotal_claims", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attributed_claims", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_claims", sa.Integer, nullable=False, server_default="0"),
        sa.Column("first_fact_ms", sa.Integer),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_analysis_runs_condition_id", "analysis_runs", ["condition_id"])
    op.create_index("ix_analysis_runs_forecast_id", "analysis_runs", ["forecast_id"])
    op.create_index("ix_analysis_runs_user_created", "analysis_runs", ["user_id", "created_at"])


def downgrade():
    op.drop_table("analysis_runs")
