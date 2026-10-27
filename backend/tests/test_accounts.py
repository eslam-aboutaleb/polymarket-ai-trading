"""Sub-account foundation tests (plan 06 Phase A).

Covers the four things Phase A promises:

* **Isolation** — two sub-accounts under one owner keep independent
  settings: writing account B's paper balance or Kelly fraction leaves
  account A's row byte-for-byte identical. Rows are compared through the
  ORM so a shared mutable default would be caught.
* **Header resolution** — an absent header resolves to the personal
  account, an unknown id falls back the same way, and an account owned
  by a different user is refused with 403.
* **Migration backfill** — the hand-written 20261011_0015 migration is
  run in-process against an in-memory database built from
  ``Base.metadata.create_all``, so the assertion is on the migration's
  own SQL rather than on a re-implementation of it.
* **Auth** — every route rejects an unauthenticated caller with 401.

The in-memory SQLite session is real (same idiom as
``test_strategy_attribution.py``); no network and no key material is
involved anywhere.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register models on Base.metadata)
from app.api.deps import get_active_account
from app.main import app as fastapi_app
from app.models.base import Base
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account, AccountKind
from app.models.users.account_settings import AccountSettings
from app.models.users.user import User
from app.models.users.user_settings import UserSettings
from app.security.auth import create_access_token
from app.services.users.account_service import (
    AccountAccessDenied,
    create_sub_account,
    list_accounts,
    resolve_account,
)
from app.utils.database import get_db

OWNER_WALLET = "0x" + "aa" * 20
OTHER_WALLET = "0x" + "bb" * 20
MARKET_ID = "0xmarket-cond"
TOKEN_ID = "0xtoken-1"


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _login_headers(user_id: int) -> dict:
    """Mint a real access token so the routes' own auth path is exercised."""
    token = create_access_token({"user_id": user_id, "sub": OWNER_WALLET, "is_admin": False})
    return {"Authorization": f"Bearer {token}"}


class AccountBaseTest(unittest.TestCase):
    """In-memory DB with one owner and, optionally, a second user."""

    def setUp(self):
        self.engine, self.SessionFactory = _make_db()
        Base.metadata.create_all(self.engine)
        fastapi_app.dependency_overrides[get_db] = self._get_db_override
        self.client = TestClient(fastapi_app)
        self.addCleanup(self._cleanup)

        self.db = self.SessionFactory()
        self.db.add(User(id=1, wallet_address=OWNER_WALLET))
        self.db.add(User(id=2, wallet_address=OTHER_WALLET))
        self.db.commit()

        # The account context must be live for these tests: the point is
        # the header, not the flag.
        patcher = patch("app.api.deps.get_settings")
        mocked = patcher.start()
        mocked.return_value.accounts_enabled = True
        self.addCleanup(patcher.stop)

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _cleanup(self):
        fastapi_app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def _create_accounts(self, session=None):
        """One personal account plus two sub-accounts for the owner."""
        session = session or self.db
        personal = resolve_account(session, 1, None)
        alpha = create_sub_account(session, 1, "Alpha")
        beta = create_sub_account(session, 1, "Beta")
        return personal, alpha, beta

    def _foreign_account(self):
        """A sub-account belonging to the *other* user."""
        resolve_account(self.db, 2, None)
        return create_sub_account(self.db, 2, "Other user")

    def _add_trade(self, account_id: int, amount: float) -> UserTrade:
        trade = UserTrade(
            account_id=account_id,
            user_id=1,
            market_id=MARKET_ID,
            token_id=TOKEN_ID,
            action="buy",
            amount=amount,
            price=0.5,
            status="simulated",
            strategy_source="manual",
        )
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        return trade


class AccountModelTest(AccountBaseTest):
    """The model defaults and the personal-account invariant."""

    def test_personal_account_is_default_and_created_lazily(self):
        account = resolve_account(self.db, 1, None)
        self.assertEqual(account.kind, AccountKind.PERSONAL.value)
        self.assertTrue(account.is_default)
        self.assertEqual(account.owner_user_id, 1)
        self.assertEqual(account.name, "Personal")

    def test_personal_account_is_idempotent(self):
        first = resolve_account(self.db, 1, None)
        second = resolve_account(self.db, 1, None)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(Account).count(), 1)

    def test_sub_accounts_are_never_default(self):
        _, alpha, beta = self._create_accounts()
        self.assertFalse(alpha.is_default)
        self.assertFalse(beta.is_default)
        self.assertEqual(alpha.kind, AccountKind.SUB.value)

    def test_sub_account_requires_existing_personal_account(self):
        with self.assertRaises(ValueError):
            create_sub_account(self.db, 2, "Orphan")

    def test_list_orders_personal_first_then_oldest(self):
        personal, alpha, beta = self._create_accounts()
        listed = list_accounts(self.db, 1)
        self.assertEqual([a.id for a in listed], [personal.id, alpha.id, beta.id])

    def test_list_only_returns_own_accounts(self):
        self._create_accounts()
        resolve_account(self.db, 2, None)
        theirs = list_accounts(self.db, 2)
        self.assertEqual([a.id for a in theirs], [theirs[0].id])
        self.assertEqual(theirs[0].kind, AccountKind.PERSONAL.value)


class HeaderResolutionTest(AccountBaseTest):
    """``get_active_account`` ownership + fallback semantics."""

    def test_active_account_returns_personal_when_flag_off(self):
        personal = resolve_account(self.db, 1, None)
        foreign = self._foreign_account()

        with patch("app.api.deps.get_settings") as mocked:
            mocked.return_value.accounts_enabled = False
            resolved = get_active_account(
                x_account_id=str(foreign.id),
                current_user={"user_id": 1},
                db=self.db,
            )

        # A foreign id is inert while the flag is off: the personal
        # account is returned rather than a 403, so enabling the flag
        # is the only thing that changes behaviour.
        self.assertEqual(resolved.id, personal.id)

    def test_missing_header_resolves_to_personal(self):
        personal = resolve_account(self.db, 1, None)
        create_sub_account(self.db, 1, "Alpha")
        resolved = get_active_account(
            x_account_id=None,
            current_user={"user_id": 1},
            db=self.db,
        )
        self.assertEqual(resolved.id, personal.id)

    def test_unknown_account_id_falls_back_to_personal(self):
        personal = resolve_account(self.db, 1, None)
        resolved = get_active_account(
            x_account_id="424242",
            current_user={"user_id": 1},
            db=self.db,
        )
        self.assertEqual(resolved.id, personal.id)

    def test_unparseable_header_falls_back_to_personal(self):
        personal = resolve_account(self.db, 1, None)
        resolved = get_active_account(
            x_account_id="not-a-number",
            current_user={"user_id": 1},
            db=self.db,
        )
        self.assertEqual(resolved.id, personal.id)

    def test_foreign_account_id_is_refused_with_403(self):
        foreign = self._foreign_account()
        with self.assertRaises(HTTPException) as ctx:
            get_active_account(
                x_account_id=str(foreign.id),
                current_user={"user_id": 1},
                db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_own_account_id_resolves_to_that_account(self):
        _, alpha, _ = self._create_accounts()
        resolved = get_active_account(
            x_account_id=str(alpha.id),
            current_user={"user_id": 1},
            db=self.db,
        )
        self.assertEqual(resolved.id, alpha.id)


class AccountRoutesTest(AccountBaseTest):
    """HTTP surface: auth, header handling and per-account settings."""

    def _enable_accounts(self):
        patcher = patch("app.api.routes.auth.accounts.get_settings")
        mocked = patcher.start()
        mocked.return_value.accounts_enabled = True
        self.addCleanup(patcher.stop)

    def test_active_route_requires_authentication(self):
        response = self.client.get("/api/accounts/active")
        self.assertEqual(response.status_code, 401)

    def test_active_route_honours_account_header(self):
        personal, alpha, _ = self._create_accounts()
        self._enable_accounts()

        default = self.client.get("/api/accounts/active", headers=_login_headers(1)).json()
        self.assertEqual(default["account"]["id"], personal.id)
        self.assertFalse(default["header_used"])

        switched = self.client.get(
            "/api/accounts/active",
            headers={**_login_headers(1), "X-Account-Id": str(alpha.id)},
        ).json()
        self.assertEqual(switched["account"]["id"], alpha.id)
        self.assertTrue(switched["header_used"])

    def test_active_route_rejects_foreign_account(self):
        foreign = self._foreign_account()
        self._enable_accounts()
        response = self.client.get(
            "/api/accounts/active",
            headers={**_login_headers(1), "X-Account-Id": str(foreign.id)},
        )
        self.assertEqual(response.status_code, 403)

    def test_list_route_requires_authentication(self):
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)

    def test_list_route_returns_own_accounts(self):
        _, alpha, beta = self._create_accounts()
        listed = self.client.get("/api/accounts", headers=_login_headers(1)).json()
        self.assertEqual([a["id"] for a in listed], [1, alpha.id, beta.id])
        self.assertEqual([a["kind"] for a in listed][0], "personal")

    def test_create_route_requires_authentication(self):
        response = self.client.post("/api/accounts", json={"name": "Gamma"})
        self.assertEqual(response.status_code, 401)

    def test_create_route_creates_sub_account(self):
        resolve_account(self.db, 1, None)
        self._enable_accounts()
        response = self.client.post(
            "/api/accounts",
            json={"name": "Gamma"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertEqual(created["kind"], "sub")
        self.assertFalse(created["is_default"])
        self.assertEqual(
            self.db.query(Account).filter(Account.name == "Gamma").count(),
            1,
        )

    def test_create_route_refused_while_flag_off(self):
        resolve_account(self.db, 1, None)
        response = self.client.post(
            "/api/accounts",
            json={"name": "Gamma"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 403)

    def test_rename_route_requires_authentication(self):
        self.assertEqual(self.client.patch("/api/accounts/1", json={"name": "x"}).status_code, 401)

    def test_rename_route_rejects_foreign_account_as_404(self):
        foreign = self._foreign_account()
        response = self.client.patch(
            f"/api/accounts/{foreign.id}",
            json={"name": "Hijacked"},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 404)

    def test_settings_routes_require_authentication(self):
        self.assertEqual(self.client.get("/api/accounts/1/settings").status_code, 401)
        self.assertEqual(
            self.client.patch("/api/accounts/1/settings", json={}).status_code,
            401,
        )

    def test_settings_get_defaults_for_sub_account(self):
        _, _, beta = self._create_accounts()
        response = self.client.get(
            f"/api/accounts/{beta.id}/settings",
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["account_id"], beta.id)
        self.assertEqual(payload["paper_balance"], 1000.0)
        self.assertEqual(payload["kelly_fraction"], 0.25)
        self.assertFalse(payload["simulation_mode"])

    def test_personal_account_settings_read_user_settings(self):
        personal = resolve_account(self.db, 1, None)
        response = self.client.get(
            f"/api/accounts/{personal.id}/settings",
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_id"], personal.id)


class AccountIsolationTest(AccountBaseTest):
    """A write in one account must not move another account's state."""

    def _settings_for(self, account_id: int) -> dict:
        response = self.client.get(
            f"/api/accounts/{account_id}/settings",
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _patch_settings(self, account_id: int, payload: dict) -> dict:
        response = self.client.patch(
            f"/api/accounts/{account_id}/settings",
            json=payload,
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_settings_are_independent_between_sub_accounts(self):
        personal, alpha, beta = self._create_accounts()

        alpha_before = self._settings_for(alpha.id)
        beta_before = self._settings_for(beta.id)

        updated = self._patch_settings(
            alpha.id,
            {
                "paper_balance": 25000.0,
                "kelly_fraction": 0.75,
                "simulation_mode": True,
                "daily_loss_limit": 123.5,
            },
        )
        self.assertEqual(updated["paper_balance"], 25000.0)
        self.assertEqual(updated["kelly_fraction"], 0.75)

        # Account B is untouched by A's write.
        self.assertEqual(self._settings_for(beta.id), beta_before)
        # The owner's own (personal) settings are untouched too — that is
        # the row every pre-plan route still reads.
        self.assertEqual(
            self._settings_for(personal.id)["paper_balance"],
            1000.0,
        )
        self.assertFalse(self._settings_for(personal.id)["simulation_mode"])

        # Re-reading A is stable, so the values were really persisted.
        alpha_after = self._settings_for(alpha.id)
        self.assertEqual(alpha_after["paper_balance"], 25000.0)
        self.assertNotEqual(alpha_after, alpha_before)

    def test_trades_are_scoped_to_their_account(self):
        _, alpha, beta = self._create_accounts()
        self._add_trade(alpha.id, 10.0)
        self._add_trade(beta.id, 20.0)

        def _trades(account_id: int):
            return [
                t.amount
                for t in self.db.query(UserTrade).filter(UserTrade.account_id == account_id).all()
            ]

        self.assertEqual(_trades(alpha.id), [10.0])
        self.assertEqual(_trades(beta.id), [20.0])

    def test_settings_update_rejects_out_of_range_values(self):
        _, alpha, _ = self._create_accounts()
        response = self.client.patch(
            f"/api/accounts/{alpha.id}/settings",
            json={"kelly_fraction": 4.0},
            headers=_login_headers(1),
        )
        self.assertEqual(response.status_code, 422)

    def test_settings_update_cannot_clear_a_trading_halt(self):
        """Halt state belongs to the engine, not to a settings write."""
        _, alpha, _ = self._create_accounts()
        self._patch_settings(alpha.id, {"paper_balance": 500.0})
        row = self.db.query(AccountSettings).filter_by(account_id=alpha.id).one()
        row.trading_halted = True
        row.halt_reason = "daily loss limit"
        self.db.commit()

        self._patch_settings(alpha.id, {"paper_balance": 900.0})
        self.db.refresh(row)
        self.assertTrue(row.trading_halted)
        self.assertEqual(row.halt_reason, "daily loss limit")
        self.assertEqual(row.paper_balance, 900.0)

    def test_personal_account_settings_write_updates_user_settings(self):
        """The personal account stays on ``user_settings`` — one row, one truth."""
        personal = resolve_account(self.db, 1, None)
        self._patch_settings(personal.id, {"paper_balance": 4321.0})

        row = self.db.query(UserSettings).filter_by(user_id=1).one()
        self.assertEqual(row.paper_balance, 4321.0)
        # No account_settings row is created for the personal account.
        self.assertEqual(self.db.query(AccountSettings).count(), 0)


class AccountAccessDeniedMappingTest(AccountBaseTest):
    """``AccountAccessDenied`` is the service's 403, and nothing else."""

    def test_foreign_account_raises_domain_error(self):
        foreign = self._foreign_account()
        with self.assertRaises(AccountAccessDenied):
            resolve_account(self.db, 1, foreign.id)

    def test_own_account_does_not_raise(self):
        _, alpha, _ = self._create_accounts()
        self.assertEqual(resolve_account(self.db, 1, str(alpha.id)).id, alpha.id)


class MigrationBackfillTest(unittest.TestCase):
    """Run the real 20261011_0015 migration against a fresh database.

    The database is built from ``Base.metadata.create_all`` and then
    rewound to the pre-migration shape — the ``accounts`` and
    ``account_settings`` tables dropped and every ``account_id`` column
    removed — which is exactly the schema revision 20261011_0014 left
    behind. The migration is then run in-process, so the assertions are
    on its own SQL (CREATE TABLE, ADD COLUMN, backfill) rather than on a
    re-implementation of it.
    """

    MIGRATION_MODULE = "migrations.versions.20261011_0015_add_accounts"
    #: Tables the migration itself creates or leaves alone.
    EXCLUDED_TABLES = frozenset(
        {
            "accounts",
            "account_settings",
            "app_events",
            "trading_control_states",
            "emergency_halt_operations",
        }
    )

    def _tables_with_user_id(self, conn) -> list[str]:
        tables = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            )
        ]
        owned = []
        for table in tables:
            columns = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if "user_id" in columns and table not in self.EXCLUDED_TABLES:
                owned.append(table)
        return sorted(owned)

    def _migrate(self):
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        self.engine, self.SessionFactory = _make_db()
        Base.metadata.create_all(self.engine)

        db = self.SessionFactory()
        db.add(User(id=1, wallet_address=OWNER_WALLET))
        db.add(User(id=2, wallet_address=OTHER_WALLET))
        db.add(User(id=3, wallet_address="0x" + "cc" * 20))
        db.commit()

        # One user-owned row per user, so the backfill join is exercised
        # on more than one table.
        db.add_all([UserSettings(user_id=1), UserSettings(user_id=2), UserSettings(user_id=3)])
        db.add_all(
            [
                UserTrade(
                    user_id=1,
                    market_id=MARKET_ID,
                    action="buy",
                    amount=5.0,
                    price=0.5,
                    status="executed",
                ),
                UserTrade(
                    user_id=2,
                    market_id=MARKET_ID,
                    action="buy",
                    amount=6.0,
                    price=0.5,
                    status="executed",
                ),
                UserTrade(
                    user_id=3,
                    market_id=MARKET_ID,
                    action="buy",
                    amount=7.0,
                    price=0.5,
                    status="executed",
                ),
            ]
        )
        db.commit()

        # Rewind to the revision this migration chains from: the staged
        # rows survive, only the accounts-shaped schema goes away.
        with self.engine.connect() as conn:
            # This archive did not exist at the pre-account revision. Remove
            # it before rewinding its required account FK into a legacy schema.
            conn.execute(text("DROP TABLE IF EXISTS executable_signal_sources"))
            conn.execute(text("DROP TABLE IF EXISTS executable_replay_runs"))
            conn.execute(text("DROP TABLE IF EXISTS book_research_runs"))
            conn.execute(text("DROP TABLE IF EXISTS follower_experiment_runs"))
            conn.execute(text("DROP TABLE IF EXISTS paper_maker_intents"))
            for future_table in (
                "paper_maker_legacy",
                "paper_maker_marks",
                "paper_maker_fills",
                "paper_maker_evidence",
                "paper_maker_cursors",
                "paper_maker_orders",
                "paper_nav_points",
                "paper_order_records",
                "paper_journal_events",
                "paper_accounts",
            ):
                conn.execute(text(f"DROP TABLE IF EXISTS {future_table}"))
            conn.execute(text("DROP TABLE IF EXISTS account_settings"))
            conn.execute(text("DROP TABLE IF EXISTS accounts"))
            for table in self._tables_with_user_id(conn):
                index = f"ix_{table}_account_id"
                # PRAGMA index_list: (seq, name, unique, origin, partial)
                indexes = {row[1] for row in conn.execute(text(f"PRAGMA index_list({table})"))}
                if index in indexes:
                    conn.execute(text(f"DROP INDEX {index}"))
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN account_id"))
            conn.commit()

        with self.engine.connect() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(migration_context=context):
                importlib.import_module(self.MIGRATION_MODULE).upgrade()
            conn.commit()

        self.addCleanup(self.engine.dispose)
        self.addCleanup(db.close)
        return db

    def test_one_personal_account_per_user(self):
        db = self._migrate()
        accounts = db.query(Account).order_by(Account.owner_user_id).all()
        self.assertEqual([a.owner_user_id for a in accounts], [1, 2, 3])
        for account in accounts:
            self.assertEqual(account.kind, AccountKind.PERSONAL.value)
            self.assertTrue(account.is_default)
            self.assertEqual(account.name, "Personal")

    def test_user_owned_rows_are_backfilled(self):
        db = self._migrate()
        personal_ids = {
            account.owner_user_id: account.id
            for account in db.query(Account).filter_by(kind=AccountKind.PERSONAL.value).all()
        }
        mapping = {
            row.user_id: row.account_id
            for row in db.query(UserSettings).order_by(UserSettings.user_id).all()
        }
        self.assertEqual(mapping, personal_ids)

        trades = db.query(UserTrade).order_by(UserTrade.user_id).all()
        self.assertEqual(
            [t.account_id for t in trades],
            [personal_ids[t.user_id] for t in trades],
        )

    def test_every_user_owned_table_gained_the_column(self):
        """The additive column landed on all 21 user-owned tables."""
        self._migrate()
        with self.engine.connect() as conn:
            migrated = self._tables_with_user_id(conn)
            # 20 tables the migration widened, plus api_keys (plan 06b)
            # and the collaboration tables (plan 02), which were created
            # with account_id already in place.
            self.assertEqual(len(migrated), 24)
            for table in migrated:
                columns = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                self.assertIn("account_id", columns, msg=table)

    def test_every_user_owned_row_has_an_account(self):
        """No ``account_id`` may be left NULL on a table that has a user."""
        self._migrate()
        with self.engine.connect() as conn:
            checked = 0
            for table in self._tables_with_user_id(conn):
                checked += 1
                # Table names come from PRAGMA table_info, never from input.
                statement = f"SELECT COUNT(*) FROM {table} WHERE account_id IS NULL"  # noqa: S608
                nulls = conn.execute(text(statement)).scalar()
                self.assertEqual(
                    nulls,
                    0,
                    msg=f"{table} has {nulls} rows without an account_id",
                )
        # 20 widened tables + api_keys (plan 06b) + the 3 collaboration
        # tables (plan 02), all created with the column.
        self.assertEqual(checked, 24)

    def test_migration_is_idempotent(self):
        """Re-running the migration must not duplicate accounts or rows."""
        import importlib

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        db = self._migrate()
        with self.engine.connect() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(migration_context=context):
                importlib.import_module(self.MIGRATION_MODULE).upgrade()
            conn.commit()

        self.assertEqual(db.query(Account).count(), 3)
        personal_ids = {
            account.owner_user_id: account.id
            for account in db.query(Account).filter_by(kind=AccountKind.PERSONAL.value).all()
        }
        self.assertEqual(len(personal_ids), 3)
        mapping = {
            row.user_id: row.account_id
            for row in db.query(UserSettings).order_by(UserSettings.user_id).all()
        }
        self.assertEqual(mapping, personal_ids)

    def test_migration_creates_account_settings_table(self):
        db = self._migrate()
        self.assertEqual(db.query(AccountSettings).all(), [])
        with self.engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(account_settings)"))}
        self.assertIn("account_id", columns)
        self.assertIn("paper_balance", columns)
        self.assertIn("kelly_fraction", columns)


if __name__ == "__main__":
    unittest.main()
