"""Account-owned frozen microstructure research recipes."""

from alembic import op

from app.models.analytics.book_research_run import BookResearchRun

revision = "20261027_0035_book_research"
down_revision = "20261027_0034_venue_observations"
branch_labels = None
depends_on = None


def upgrade():
    BookResearchRun.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    BookResearchRun.__table__.drop(op.get_bind(), checkfirst=True)
