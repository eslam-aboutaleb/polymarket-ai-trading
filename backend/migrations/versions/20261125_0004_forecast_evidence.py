"""Content-addressed evidence snapshots for generated forecasts (issue #4).

Revision ID: 20261125_0004_forecast_evidence
Revises: 20261125_0060_forecast_identity
Create Date: 2026-11-25 19:40:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0004_forecast_evidence"
down_revision = "20261125_0060_forecast_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("forecast_evidence_snapshots"):
        op.create_table(
            "forecast_evidence_snapshots",
            sa.Column("snapshot_id", sa.String(64), primary_key=True),
            sa.Column("payload_json", sa.Text, nullable=False),
            sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
            sa.Column("generator_version", sa.String(40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    # Forecast rows keep their evidence_snapshot_id hash; only the stored text is dropped.
    if sa.inspect(op.get_bind()).has_table("forecast_evidence_snapshots"):
        op.drop_table("forecast_evidence_snapshots")
