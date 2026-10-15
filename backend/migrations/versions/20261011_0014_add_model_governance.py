"""Model governance: assessment provenance columns + feedback and experiments (plan 04).

Makes AI verdict quality measurable and governable:

* ``assessments`` gains ``prompt_version``, ``experiment_id``,
  ``variant`` and ``llm_tier`` so every verdict is attributable to the
  prompt and the model config that produced it.
* ``verdict_feedback`` captures a trader's 1–5 rating of a verdict.
* ``llm_experiments`` holds one A/B arm split per task, with a partial
  unique index guaranteed by raw SQL so at most one experiment is
  active per task.

Chains after the app_events migration (20261011_0013) to keep a single
linear alembic head:
..._0013 → 20261011_0014 (this).

The incomplete legacy rows (plan 01) are excluded from every
governance aggregate, not backfilled: the new columns stay NULL for
them, exactly like the other plan-04 observability columns.

Revision ID: 20261011_0014
Revises: 20261011_0013
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0014"
down_revision = "20261011_0013"
branch_labels = None
depends_on = None

# (column_name, type) pairs added to assessments.
_GOVERNANCE_COLUMNS = [
    ("prompt_version", sa.String(length=16)),
    ("experiment_id", sa.String(length=64)),
    ("variant", sa.String(length=16)),
    ("llm_tier", sa.String(length=16)),
]


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_columns("assessments")
    for name, col_type in _GOVERNANCE_COLUMNS:
        if name not in existing:
            op.add_column("assessments", sa.Column(name, col_type, nullable=True))

    tables = _existing_tables()

    if "verdict_feedback" not in tables:
        op.create_table(
            "verdict_feedback",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("assessment_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("rating", sa.SmallInteger(), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["assessment_id"], ["assessments.id"]),
            sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_verdict_feedback_rating"),
        )
        op.create_index("ix_verdict_feedback_id", "verdict_feedback", ["id"])
        op.create_index("ix_verdict_feedback_assessment_id", "verdict_feedback", ["assessment_id"])
        op.create_index("ix_verdict_feedback_user_id", "verdict_feedback", ["user_id"])
        op.create_index("ix_verdict_feedback_created_at", "verdict_feedback", ["created_at"])

    if "llm_experiments" not in tables:
        op.create_table(
            "llm_experiments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("task", sa.String(length=50), nullable=False),
            sa.Column("control_config_json", sa.Text(), nullable=True),
            sa.Column("variant_config_json", sa.Text(), nullable=True),
            sa.Column("traffic_pct", sa.Integer(), nullable=False, server_default=sa.text("10")),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
            sa.Column("created_by", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_llm_experiments_id", "llm_experiments", ["id"])
        op.create_index("ix_llm_experiments_task", "llm_experiments", ["task"])
        # A unique index, not op.create_unique_constraint: SQLite cannot
        # ADD a UNIQUE constraint, which made the re-upgrade branch of
        # the chain test fail with NotImplementedError. The index is the
        # portable spelling and matches the model's __table_args__.
        op.create_index("uq_llm_experiments_name", "llm_experiments", ["name"], unique=True)

    # At most one ACTIVE experiment per task. Written as raw SQL because
    # ``sa.Index(..., unique=True, sqlite_where=...)`` needs the flag
    # form that differs across backends; CREATE INDEX has no
    # IF NOT EXISTS portable form, so the guard is a name probe.
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        index_names = {
            row[0]
            for row in bind.execute(
                sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'llm_experiments'")
            )
        }
        if "uq_llm_experiments_active_task" not in index_names:
            op.execute(
                "CREATE UNIQUE INDEX uq_llm_experiments_active_task "
                "ON llm_experiments (task) WHERE status = 'active'"
            )
    # SQLite has no partial indexes; the active-only invariant is
    # enforced in application code there (see the governance service).


def downgrade() -> None:
    bind = op.get_bind()
    if "llm_experiments" in _existing_tables():
        op.drop_index("uq_llm_experiments_name", table_name="llm_experiments")
        op.drop_table("llm_experiments")
    if "verdict_feedback" in _existing_tables():
        op.drop_table("verdict_feedback")
    existing = _existing_columns("assessments")
    for name, _ in reversed(_GOVERNANCE_COLUMNS):
        if name in existing:
            op.drop_column("assessments", name)
