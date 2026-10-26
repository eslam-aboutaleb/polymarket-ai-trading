"""Append-only public stream observation archive."""

from alembic import op

from app.models.market_data.venue_observation import (
    ObservationDataset,
    ObservationDatasetMember,
    VenueObservation,
)

revision = "20261027_0034_venue_observations"
down_revision = "20261024_0033_live_orders"
branch_labels = None
depends_on = None


def upgrade():
    for model in (VenueObservation, ObservationDataset, ObservationDatasetMember):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (ObservationDatasetMember, ObservationDataset, VenueObservation):
        model.__table__.drop(op.get_bind(), checkfirst=True)
