"""Durable real-order identities, reservations and exchange reconciliation."""

from alembic import op

from app.models.trading.live_order import LiveOrder

revision = "20261024_0033_live_orders"
down_revision = "20261023_0032_native_replay_capture"
branch_labels = None
depends_on = None


def upgrade():
    LiveOrder.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    LiveOrder.__table__.drop(op.get_bind())
