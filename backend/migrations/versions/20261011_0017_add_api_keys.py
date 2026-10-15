"""API keys for the external API (plan 06 Phase B).

``api_keys`` is how a non-browser client authenticates against
``/api/v1/external/*``. It belongs to one ``User`` and, when the caller
scoped it, to one of that user's ``Account`` rows — an external key is
never a second identity, it is the same owner acting through a channel
that cannot hold a cookie.

Security posture as designed:

* only an HMAC-SHA256 **digest** is persisted; the raw secret is shown
  once at creation and can never be re-derived from a row, so leaking
  the table leaks no usable credential,
* ``key_prefix`` is the public half used for an indexed lookup; the
  digest column is unique so a comparison is a single indexed row,
* scopes are an allow-list of ``read``/``trade`` — there is no
  ``withdraw`` scope, so no key can move funds the UI could not,
* ``revoked_at``/``expires_at`` are checked on every authentication.

``account_id`` is nullable per master plan 5.1 (additive; accounts land
in plan 06 Phase A) and is NOT a foreign key here: deleting an account
is not a supported operation, and a cascade would silently orphan a
live credential.

Chains after the venue-markets migration (20261011_0016) to keep a
single linear alembic head:
..._0016 → 20261011_0017 (this).

Revision ID: 20261011_0017
Revises: 20261011_0016
Create Date: 2026-10-12 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0017"
down_revision = "20261011_0016"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "api_keys" in _existing_tables():
        return

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        # Additive + nullable (master 5.1): no FK, so backfilling and
        # later account work never rewrite this table.
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_api_keys_id", "api_keys", ["id"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_account_id", "api_keys", ["account_id"])
    # Unique, so an authentication is one indexed lookup and two keys can
    # never collide.
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    op.create_index("ix_api_keys_user_created", "api_keys", ["user_id", "created_at"])


def downgrade() -> None:
    if "api_keys" not in _existing_tables():
        return
    op.drop_index("ix_api_keys_user_created", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_index("ix_api_keys_account_id", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_id", table_name="api_keys")
    op.drop_table("api_keys")
