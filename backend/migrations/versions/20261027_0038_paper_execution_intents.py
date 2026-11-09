"""Durable paper decisions, separate from shared journal outcomes."""

from alembic import op

revision = "20261027_0038_paper_intents"
down_revision = "20261027_0037_executable_replay"
branch_labels = None
depends_on = None


def upgrade():
    from app.models import Base

    Base.metadata.tables["paper_execution_intents"].create(op.get_bind(), checkfirst=True)


def downgrade():
    from app.models import Base

    Base.metadata.tables["paper_execution_intents"].drop(op.get_bind(), checkfirst=True)
