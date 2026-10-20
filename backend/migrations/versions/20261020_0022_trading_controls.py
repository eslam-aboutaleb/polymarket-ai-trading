"""Persist a fail-closed release halt and restartable cancellation work.

Revision ID: 20261020_0022_trading_controls
Revises: 20261016_0021_drop_redundant_pk_indexes
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "20261020_0022_trading_controls"
down_revision = "20261016_0021_drop_redundant_pk_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    # The historical baseline bootstraps current Base.metadata on fresh DBs.
    # Existing deployments still need the explicit expand migration below.
    if "trading_control_states" not in tables:
        op.create_table(
            "trading_control_states",
            sa.Column("scope", sa.String(80), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
            sa.Column("state", sa.String(30), nullable=False),
            sa.Column("reason", sa.String(500), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('RUNNING','HALTING','HALTED','RECOVERY_REQUIRED')",
                name="ck_trading_control_state",
            ),
        )
        op.create_index("ix_trading_control_states_user_id", "trading_control_states", ["user_id"])
    if "emergency_halt_operations" not in tables:
        op.create_table(
            "emergency_halt_operations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("requested_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("state", sa.String(30), nullable=False),
            sa.Column("close_positions", sa.Boolean(), nullable=False),
            sa.Column("control_version", sa.Integer(), nullable=False),
            sa.Column("report", sa.JSON(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('PENDING','VERIFIED','RECOVERY_REQUIRED')",
                name="ck_emergency_operation_state",
            ),
        )
        op.create_index(
            "ix_emergency_halt_operations_user_id", "emergency_halt_operations", ["user_id"]
        )
        op.create_index(
            "ix_emergency_halt_operations_state", "emergency_halt_operations", ["state"]
        )
    controls = sa.Table("trading_control_states", sa.MetaData(), autoload_with=bind)
    if bind.execute(sa.select(controls.c.scope).where(controls.c.scope == "global")).first():
        return  # Never overwrite a pre-existing halt during a repeat migration.
    op.bulk_insert(
        controls,
        [
            {
                "scope": "global",
                "user_id": None,
                "state": "RECOVERY_REQUIRED",
                "reason": "Live release blocked pending durable execution and custody verification",
                "version": 1,
                "updated_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    # A rollback must not destroy active halt evidence or pending operations.
    bind = op.get_bind()
    active = bind.execute(sa.text("SELECT count(*) FROM emergency_halt_operations")).scalar_one()
    modified = bind.execute(
        sa.text(
            "SELECT count(*) FROM trading_control_states "
            "WHERE scope <> 'global' OR version <> 1 OR state <> 'RECOVERY_REQUIRED'"
        )
    ).scalar_one()
    if active or modified:
        raise RuntimeError("Cannot remove durable trading controls with halt history")
    op.drop_table("emergency_halt_operations")
    op.drop_table("trading_control_states")
