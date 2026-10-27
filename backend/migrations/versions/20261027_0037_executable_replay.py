"""Frozen executable-research recipes and output; no live order changes."""

from alembic import op

revision = "20261027_0037_executable_replay"
down_revision = "20261026_0034_paper_accounts"
branch_labels = None
depends_on = None


def upgrade():
    from app.models import Base

    for name in ("executable_replay_runs", "executable_signal_sources"):
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    from app.models import Base

    for name in ("executable_signal_sources", "executable_replay_runs"):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
