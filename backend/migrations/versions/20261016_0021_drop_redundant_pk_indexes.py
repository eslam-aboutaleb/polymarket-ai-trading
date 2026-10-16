"""Drop standalone indexes that duplicate integer primary keys.

The venue-market and API-key table migrations predate the current model
layout and created explicit ``id`` indexes in addition to their primary
keys. The primary-key indexes already support those lookups, so these
secondary indexes add write and storage cost without improving reads.

Revision ID: 20261016_0021_drop_redundant_pk_indexes
Revises: 20261015_0020_add_perf_indexes
Create Date: 2026-10-16 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261016_0021_drop_redundant_pk_indexes"
down_revision = "20261015_0020_add_perf_indexes"
branch_labels = None
depends_on = None

_REDUNDANT_INDEXES = (
    ("ix_venue_markets_id", "venue_markets"),
    ("ix_api_keys_id", "api_keys"),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for index_name, table_name in _REDUNDANT_INDEXES:
        if table_name not in tables:
            continue
        indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        if index_name in indexes:
            op.drop_index(index_name, table_name=table_name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for index_name, table_name in _REDUNDANT_INDEXES:
        if table_name not in tables:
            continue
        indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        if index_name not in indexes:
            op.create_index(index_name, table_name, ["id"], unique=False)
