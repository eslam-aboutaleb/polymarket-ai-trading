"""Immutable forecast identity, benchmark provenance and resolution audit (issue #60).

Revision ID: 20261125_0060_forecast_identity
Revises: 20261122_0042_studies
Create Date: 2026-11-25 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261125_0060_forecast_identity"
down_revision = "20261122_0042_studies"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("forecast_uuid", sa.String(36)),
    ("request_key", sa.String(64)),
    ("token_id", sa.String(100)),
    ("p_interpretation", sa.String(24)),
    ("outcome_mapping_json", sa.Text()),
    ("rule_hash", sa.String(64)),
    ("evidence_snapshot_id", sa.String(64)),
    ("calibrator_version", sa.String(32)),
    ("prediction_instant", sa.DateTime(timezone=True)),
    ("market_close_at", sa.DateTime(timezone=True)),
    ("horizon_seconds", sa.Integer()),
    ("event_group", sa.String(100)),
    ("benchmark_price", sa.Float()),
    ("benchmark_bid", sa.Float()),
    ("benchmark_ask", sa.Float()),
    ("benchmark_observed_at", sa.DateTime(timezone=True)),
    ("benchmark_source", sa.String(40)),
    ("quote_age_seconds", sa.Float()),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c["name"] for c in inspector.get_columns("assessments")}
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column("assessments", sa.Column(name, column_type, nullable=True))
    indexes = {i["name"] for i in inspector.get_indexes("assessments")}
    for name in ("forecast_uuid", "request_key"):
        index = f"ix_assessments_{name}"
        if index not in indexes:
            # Unique: legacy rows stay NULL and NULLs never collide.
            op.create_index(index, "assessments", [name], unique=True)
    if not inspector.has_table("forecast_resolution_events"):
        op.create_table(
            "forecast_resolution_events",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("assessment_id", sa.Integer, sa.ForeignKey("assessments.id"), nullable=False),
            sa.Column("condition_id", sa.String(100)),
            sa.Column("event_type", sa.String(20), nullable=False),
            sa.Column("scoring_version", sa.String(32)),
            sa.Column("resolved_outcome", sa.String(20)),
            sa.Column("brier_score", sa.String(32)),
            sa.Column("exclusion_reason", sa.String(40)),
            sa.Column("finality_status", sa.String(32)),
            sa.Column("rule_hash", sa.String(64)),
            sa.Column("detail_json", sa.Text),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_forecast_resolution_events_assessment",
            "forecast_resolution_events",
            ["assessment_id"],
        )
        op.create_index(
            "ix_forecast_resolution_events_condition",
            "forecast_resolution_events",
            ["condition_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("forecast_resolution_events"):
        op.drop_table("forecast_resolution_events")
    indexes = {i["name"] for i in inspector.get_indexes("assessments")}
    for name in ("request_key", "forecast_uuid"):
        if f"ix_assessments_{name}" in indexes:
            op.drop_index(f"ix_assessments_{name}", table_name="assessments")
    existing = {c["name"] for c in inspector.get_columns("assessments")}
    for name, _type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("assessments", name)
