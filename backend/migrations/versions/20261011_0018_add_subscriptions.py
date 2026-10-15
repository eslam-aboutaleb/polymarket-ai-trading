"""Subscriptions and the billing webhook ledger (plan 09 Phase A).

Two additive tables and one backfill, in this order:

* ``subscriptions`` — one row per account carrying the plan tier that
  governs entitlements. The plan lives on the **account** per master plan
  5.1, so a sub-account resolves its own tier through its own row and a
  change made on a sub-account never touches the owner's personal one.
  There is no ``user_id`` column: the owner is reachable through
  ``accounts.owner_user_id``, and a second copy of the owner is a second
  thing to keep in sync.
* ``billing_webhook_events`` — the idempotency key for provider webhooks.
  A unique ``event_id`` means a replayed delivery matches an existing row
  and changes nothing, which is what makes the unauthenticated webhook
  endpoint safe to expose: the signature is the auth, and this table is
  the de-duplication.
* **Backfill** — every existing account gains a ``free`` row. This is the
  part that must not be rushed: without it, the first request after deploy
  would resolve "no subscription" for accounts created before the table
  existed. Free is the correct default because the free tier is exactly
  "what the product did before billing existed": manual trading, market
  browsing and basic analysis, all of which stay ungated.

Nothing is dropped, rewritten or renamed, and no existing column changes.
The migration is idempotent via ``sa.inspect`` so a partially applied run
(a crash between the two CREATE TABLEs) can be re-run safely.

Chains after the API-key migration (20261011_0017) to keep a single linear
alembic head: ..._0017 → 20261011_0018 (this).

Revision ID: 20261011_0018
Revises: 20261011_0017
Create Date: 2026-10-12 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0018"
down_revision = "20261011_0017"
branch_labels = None
depends_on = None


def _existing_tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _existing_tables()

    if "subscriptions" not in tables:
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.Integer(), nullable=False),
            # The account owns the entitlement; the owner is reachable
            # through ``accounts.owner_user_id``, so there is no user_id
            # column to keep in sync.
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column(
                "plan",
                sa.String(length=20),
                nullable=False,
                server_default="free",
            ),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="none",
            ),
            sa.Column(
                "provider",
                sa.String(length=20),
                nullable=False,
                server_default="none",
            ),
            sa.Column("provider_customer_id", sa.String(length=120), nullable=True),
            sa.Column("provider_subscription_id", sa.String(length=120), nullable=True),
            sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "cancel_at_period_end",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
            sa.CheckConstraint(
                "plan IN ('free', 'pro', 'institutional')",
                name="ck_subscriptions_plan",
            ),
            sa.CheckConstraint(
                "status IN ('none', 'active', 'trialing', 'past_due', 'canceled', 'incomplete')",
                name="ck_subscriptions_status",
            ),
            sa.CheckConstraint(
                "provider IN ('none', 'stripe')",
                name="ck_subscriptions_provider",
            ),
        )
        op.create_index("ix_subscriptions_id", "subscriptions", ["id"])
        op.create_index("ix_subscriptions_account_id", "subscriptions", ["account_id"])
        # Unique so a webhook naming a provider subscription resolves to
        # exactly one row. NULL is exempt in both SQLite and Postgres, so
        # every backfilled free account coexists.
        op.create_index(
            "ix_subscriptions_provider_subscription_id",
            "subscriptions",
            ["provider_subscription_id"],
            unique=True,
        )

    if "billing_webhook_events" not in tables:
        op.create_table(
            "billing_webhook_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.String(length=120), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False, server_default=""),
            sa.Column(
                "provider",
                sa.String(length=20),
                nullable=False,
                server_default="stripe",
            ),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("plan", sa.String(length=20), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        )
        op.create_index(
            "ix_billing_webhook_events_id",
            "billing_webhook_events",
            ["id"],
        )
        op.create_index(
            "ix_billing_webhook_events_event_id",
            "billing_webhook_events",
            ["event_id"],
            unique=True,
        )
        op.create_index(
            "ix_billing_webhook_events_account_id",
            "billing_webhook_events",
            ["account_id"],
        )

    _backfill_free_subscriptions()


def _backfill_free_subscriptions() -> None:
    """Give every account a ``free`` subscription.

    ``INSERT ... SELECT`` driven so an account with no other rows still
    gets exactly one subscription, and guarded by ``NOT EXISTS`` so a
    re-run (or a partially applied migration) adds nothing.

    ``CURRENT_TIMESTAMP`` rather than a literal so the same SQL renders on
    SQLite and Postgres; it also satisfies the two ``NOT NULL`` timestamp
    columns this row gains.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO subscriptions "
            "(account_id, plan, status, provider, cancel_at_period_end, "
            " created_at, updated_at) "
            "SELECT a.id, 'free', 'none', 'none', :cancel_at_period_end, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM accounts a "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM subscriptions s WHERE s.account_id = a.id"
            ") "
            "ORDER BY a.id"
        ),
        {"cancel_at_period_end": False},
    )


def downgrade() -> None:
    tables = _existing_tables()
    if "billing_webhook_events" in tables:
        op.drop_index(
            "ix_billing_webhook_events_account_id",
            table_name="billing_webhook_events",
        )
        op.drop_index(
            "ix_billing_webhook_events_event_id",
            table_name="billing_webhook_events",
        )
        op.drop_index("ix_billing_webhook_events_id", table_name="billing_webhook_events")
        op.drop_table("billing_webhook_events")
    if "subscriptions" in tables:
        op.drop_index(
            "ix_subscriptions_provider_subscription_id",
            table_name="subscriptions",
        )
        op.drop_index("ix_subscriptions_account_id", table_name="subscriptions")
        op.drop_index("ix_subscriptions_id", table_name="subscriptions")
        op.drop_table("subscriptions")
