"""Durable maker lifecycle; follows integrated shared journal dependency."""

from alembic import op

revision = "20261027_0036_maker_lifecycle"
down_revision = "20261026_0034_paper_accounts"
branch_labels = None
depends_on = None


def upgrade():
    from app.models import Base
    from app.models.trading import paper_maker_order  # noqa: F401

    for name in (
        "paper_maker_orders",
        "paper_maker_cursors",
        "paper_maker_evidence",
        "paper_maker_fills",
        "paper_maker_marks",
        "paper_maker_legacy",
    ):
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    from app.models import Base

    for name in (
        "paper_maker_legacy",
        "paper_maker_marks",
        "paper_maker_fills",
        "paper_maker_evidence",
        "paper_maker_cursors",
        "paper_maker_orders",
    ):
        Base.metadata.tables[name].drop(op.get_bind(), checkfirst=True)
