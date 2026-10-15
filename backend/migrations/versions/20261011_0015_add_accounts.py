"""Sub-accounts foundation: accounts + account_settings (plan 06 Phase A).

Three additive changes, in this order:

* ``accounts`` — the single place trading ownership lives:
  ``(id, owner_user_id, name, kind, is_default, created_at)``.
* ``account_settings`` — a ``user_settings``-shaped row per non-personal
  account, so a sub-account's risk limits and paper balance are
  independent of the owner's. ``user_settings`` itself is untouched.
* ``account_id INTEGER NULL`` on every table that already carries a raw
  ``user_id``, backfilled to the owner's own personal account.

The backfill is the part that must not be rushed: one ``personal``
account is inserted per existing user (``INSERT ... SELECT`` so a
user with no rows still gets one), then each table's ``account_id`` is
copied from it by joining on ``user_id``. Reads therefore stay correct
with no application change — a pre-account row maps to the personal
account its owner would have been switched to anyway.

The column is additive and nullable, never NOT NULL: a partial rollout
must still be readable (master plan 5.1). Nothing is dropped or
rewritten. Idempotent via ``sa.inspect`` so a re-run against a database
that already has the column is a no-op.

Chains after the model-governance migration (20261011_0014) to keep a
single linear alembic head: ..._0014 → 20261011_0015 (this).

Revision ID: 20261011_0015
Revises: 20261011_0014
Create Date: 2026-10-11 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261011_0015"
down_revision = "20261011_0014"
branch_labels = None
depends_on = None

# ``app_events`` already gained an account_id column in 20261011_0013 and
# is excluded from both the add and the backfill: it is a log, and its
# pre-account rows belong to no account at all.
_SKIP_TABLES = frozenset({"accounts", "account_settings", "app_events"})


def _inspector():
    return sa.inspect(op.get_bind())


def _existing_tables() -> set[str]:
    return set(_inspector().get_table_names())


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in _inspector().get_columns(table)}


def _user_owned_tables() -> list[str]:
    """Every table that carries a raw ``user_id`` column today.

    Enumerated from the live schema rather than a hard-coded list, so a
    table added by a later plan is picked up automatically and a table
    renamed out of existence cannot make the backfill fail.
    """
    return sorted(
        table
        for table in _existing_tables()
        if table not in _SKIP_TABLES and "user_id" in _existing_columns(table)
    )


def upgrade() -> None:
    tables = _existing_tables()

    if "accounts" not in tables:
        op.create_table(
            "accounts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column(
                "kind",
                sa.String(length=20),
                nullable=False,
                server_default="personal",
            ),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
            sa.CheckConstraint("kind IN ('personal', 'sub')", name="ck_accounts_kind"),
        )
        op.create_index("ix_accounts_id", "accounts", ["id"])
        op.create_index("ix_accounts_owner_user_id", "accounts", ["owner_user_id"])

    if "account_settings" not in tables:
        op.create_table(
            "account_settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("ai_backend", sa.String(length=20), nullable=False),
            sa.Column("preferred_llm_provider", sa.String(length=30), nullable=True),
            sa.Column("preferred_llm_model", sa.String(length=100), nullable=True),
            sa.Column("copy_trading_enabled", sa.Boolean(), nullable=False),
            sa.Column("risk_mode", sa.String(length=30), nullable=False),
            sa.Column("max_position_size", sa.Float(), nullable=True),
            sa.Column("daily_loss_limit", sa.Float(), nullable=True),
            sa.Column("mirror_percentage", sa.Float(), nullable=True),
            sa.Column("fixed_trade_amount", sa.Float(), nullable=True),
            sa.Column("kelly_fraction", sa.Float(), nullable=False),
            sa.Column("require_ai_approval", sa.Boolean(), nullable=False),
            sa.Column("follow_email_notifications_enabled", sa.Boolean(), nullable=False),
            sa.Column("daily_digest_enabled", sa.Boolean(), nullable=False),
            sa.Column("monthly_loss_limit", sa.Float(), nullable=True),
            sa.Column("max_drawdown_pct", sa.Float(), nullable=False),
            sa.Column("total_loss_halt_pct", sa.Float(), nullable=False),
            sa.Column("peak_capital", sa.Float(), nullable=True),
            sa.Column("initial_capital", sa.Float(), nullable=True),
            sa.Column("trading_halted", sa.Boolean(), nullable=False),
            sa.Column("halt_reason", sa.String(length=200), nullable=True),
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dynamic_sizing_enabled", sa.Boolean(), nullable=False),
            sa.Column("consecutive_wins", sa.Integer(), nullable=False),
            sa.Column("consecutive_losses", sa.Integer(), nullable=False),
            sa.Column("simulation_mode", sa.Boolean(), nullable=False),
            sa.Column("paper_balance", sa.Float(), nullable=False),
            sa.Column("inverse_bot_enabled", sa.Boolean(), nullable=False),
            sa.Column("inverse_bot_default_size_mode", sa.String(length=20), nullable=False),
            sa.Column("inverse_bot_fixed_amount", sa.Float(), nullable=False),
            sa.Column("inverse_bot_confidence_threshold", sa.Integer(), nullable=False),
            sa.Column("inverse_bot_cooldown_minutes", sa.Integer(), nullable=False),
            sa.Column("inverse_bot_max_reversals_per_day", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
            sa.UniqueConstraint("account_id", name="uq_account_settings_account_id"),
            sa.CheckConstraint(
                "(mirror_percentage IS NULL OR "
                "(mirror_percentage >= 0 AND mirror_percentage <= 100))",
                name="ck_account_settings_mirror_percentage_range",
            ),
            sa.CheckConstraint(
                "(inverse_bot_confidence_threshold >= 0 AND "
                "inverse_bot_confidence_threshold <= 100)",
                name="ck_account_settings_inverse_confidence_range",
            ),
            sa.CheckConstraint(
                "(max_position_size IS NULL OR max_position_size >= 0)",
                name="ck_account_settings_max_position_size_non_negative",
            ),
            sa.CheckConstraint(
                "(daily_loss_limit IS NULL OR daily_loss_limit >= 0)",
                name="ck_account_settings_daily_loss_limit_non_negative",
            ),
            sa.CheckConstraint(
                "(fixed_trade_amount IS NULL OR fixed_trade_amount >= 0)",
                name="ck_account_settings_fixed_trade_amount_non_negative",
            ),
            sa.CheckConstraint(
                "(inverse_bot_fixed_amount >= 0)",
                name="ck_account_settings_inverse_fixed_amount_non_negative",
            ),
            sa.CheckConstraint(
                "(inverse_bot_cooldown_minutes >= 0)",
                name="ck_account_settings_inverse_cooldown_non_negative",
            ),
            sa.CheckConstraint(
                "(inverse_bot_max_reversals_per_day >= 0)",
                name="ck_account_settings_inverse_max_reversals_non_negative",
            ),
            sa.CheckConstraint(
                "(kelly_fraction >= 0 AND kelly_fraction <= 1)",
                name="ck_account_settings_kelly_fraction_range",
            ),
        )
        op.create_index("ix_account_settings_id", "account_settings", ["id"])
        op.create_index("ix_account_settings_account_id", "account_settings", ["account_id"])

    owned = _user_owned_tables()
    for table in owned:
        if "account_id" in _existing_columns(table):
            continue
        # batch_alter_table is required on SQLite: the backend cannot
        # ALTER TABLE ADD COLUMN inside a transaction that also rebuilds
        # the table, and the copy-and-move dance is what keeps the
        # operation atomic there.
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
            batch.create_index(f"ix_{table}_account_id", ["account_id"])

    _backfill_personal_accounts()


def _backfill_personal_accounts() -> None:
    """Give every user a personal account and point their rows at it.

    Two statements, deliberately: the INSERT is ``SELECT``-driven so a
    user who owns no rows at all still gets exactly one personal
    account, and the UPDATEs run afterwards so they can join against a
    fully populated ``accounts`` table.

    Idempotent: the INSERT only fires for owners with no personal
    account yet, so a re-run (or a partially applied migration) leaves
    the mapping exactly as the first run left it.

    ``CURRENT_TIMESTAMP`` and a bound boolean are used instead of
    literals so the same SQL renders correctly on SQLite and Postgres.
    """
    bind = op.get_bind()

    bind.execute(
        sa.text(
            "INSERT INTO accounts (owner_user_id, name, kind, is_default, created_at) "
            "SELECT u.id, 'Personal', 'personal', :is_default, CURRENT_TIMESTAMP "
            "FROM users u "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM accounts a "
            "  WHERE a.owner_user_id = u.id AND a.kind = 'personal'"
            ") "
            "ORDER BY u.id"
        ),
        {"is_default": True},
    )

    for table in _user_owned_tables():
        # The table name is the only interpolated value and it comes from
        # the inspected schema, never from user input.
        statement = (
            f"UPDATE {table} SET account_id = ("  # noqa: S608 - checked-in table name
            "  SELECT a.id FROM accounts a "
            f"  WHERE a.owner_user_id = {table}.user_id AND a.kind = 'personal'"
            ") "
            "WHERE account_id IS NULL"
        )
        bind.execute(sa.text(statement))


def downgrade() -> None:
    if "account_settings" in _existing_tables():
        op.drop_table("account_settings")
    if "accounts" in _existing_tables():
        op.drop_table("accounts")
    for table in reversed(_user_owned_tables()):
        if "account_id" not in _existing_columns(table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_account_id")
            batch.drop_column("account_id")
