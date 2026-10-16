"""API-key tests (plan 06 Phase B): service, dependency and management routes.

The contract under test is the one that decides whether a leaked key is
an inconvenience or a catastrophe:

* **Show once.** The raw secret exists only in the create response. The
  row holds a prefix and an HMAC digest, and no serialiser ever emits
  either the secret or the hash again.
* **Hash-then-store.** Two rows with the same secret hash to the same
  digest, a different secret hashes somewhere else, and authentication
  compares digests rather than secrets.
* **Constant-time, prefix-indexed lookup** — including when more than
  one row shares a prefix.
* **Revocation and expiry are absolute**, and the four rejection cases
  (absent, malformed, unknown, revoked/expired) are indistinguishable
  from the caller's side.
* **Scope separation** — a ``read`` key is refused on ``POST /orders``
  with a 403, while a bad key is a 401.
* **The management surface is internal.** It authenticates with the
  session cookie/JWT, never with an API key, so a key cannot mint,
  extend or revoke another key.

The management routes are exercised on a minimal app that includes the
module's own routers (the same idiom as ``test_risk_report_routes.py``),
so these tests pass with or without the coordinator's
``app.include_router`` in ``app/main.py``.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import app.api.routes.platform.external as external_module
import app.models  # noqa: F401  (register models on Base.metadata)
from app.api.deps import external_api_rate_limit_key, require_api_key
from app.api.routes.auth.auth import get_current_user_from_token
from app.models.api_key import ApiKey
from app.models.base import Base
from app.models.user import User
from app.services.users import api_key_service
from app.services.users.api_key_service import (
    KEY_PREFIX_LENGTH,
    SCOPES,
    AuthContext,
    authenticate_api_key,
    create_api_key,
    hash_api_key,
    list_api_keys,
    normalize_scopes,
    parse_scopes,
    revoke_api_key,
    serialize_api_key,
)
from app.utils.database import get_db
from app.utils.time import utc_now


def _naive(moment):
    """SQLite hands back naive datetimes; compare like with like."""
    return moment.replace(tzinfo=None) if moment.tzinfo else moment


WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20
HASH_SECRET = "k" * 32


@contextmanager
def _configured_hash_secret(secret: str = HASH_SECRET):
    """Point the HMAC at a known test secret."""
    with patch.object(
        api_key_service,
        "get_settings",
        return_value=SimpleNamespace(api_key_hash_secret=secret, jwt_secret_key=""),
    ):
        yield


class _ApiKeyBase(unittest.TestCase):
    """In-memory database with two users and a configured hash secret."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self.db = self.SessionFactory()
        self.db.add(User(id=1, wallet_address=WALLET))
        self.db.add(User(id=2, wallet_address=OTHER_WALLET))
        self.db.commit()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        self.engine.dispose()

    def _make_key(self, **kwargs) -> tuple[ApiKey, str]:
        kwargs.setdefault("user_id", 1)
        with _configured_hash_secret():
            return create_api_key(self.db, **kwargs)


# ── Service ─────────────────────────────────────────────────


class HashThenStoreTest(_ApiKeyBase):
    def test_secret_is_returned_once_and_never_persisted(self):
        with _configured_hash_secret():
            record, secret = self._make_key(name="bot")
            self.assertTrue(secret.startswith("pk_"))
            self.assertNotIn(secret, record.scopes)
            # The only copies that persist are the public prefix and the digest.
            self.assertEqual(record.key_prefix, secret[:KEY_PREFIX_LENGTH])
            self.assertEqual(record.key_hash, hash_api_key(secret))
            self.assertNotEqual(record.key_hash, secret)

    def test_serialiser_never_exposes_secret_or_hash(self):
        record, secret = self._make_key(name="bot")
        payload = serialize_api_key(record)
        self.assertNotIn("secret", payload)
        self.assertNotIn("key_hash", payload)
        self.assertNotIn(secret, str(payload))
        self.assertEqual(payload["key_prefix"], record.key_prefix)
        self.assertEqual(payload["scopes"], ["read"])

    def test_secret_only_returned_when_explicitly_requested(self):
        record, secret = self._make_key(name="bot")
        self.assertEqual(serialize_api_key(record, include_secret=secret)["secret"], secret)

    def test_hashes_are_deterministic_per_secret_and_secret_per_key(self):
        with _configured_hash_secret():
            self.assertEqual(hash_api_key("pk_abc"), hash_api_key("pk_abc"))
            self.assertNotEqual(hash_api_key("pk_abc"), hash_api_key("pk_abd"))
            with self.assertRaises(ValueError):
                hash_api_key("")

    def test_default_expiry_and_max_lifetime(self):
        record, _ = self._make_key(name="bot")
        self.assertIsNotNone(record.expires_at)
        self.assertGreater(_naive(record.expires_at), _naive(utc_now()))

        record, _ = self._make_key(name="bot", expires_in_days=99999)
        self.assertLessEqual((_naive(record.expires_at) - _naive(utc_now())).days, 365)

    def test_account_scoped_key_stores_its_account(self):
        record, _ = self._make_key(name="bot", account_id=7)
        self.assertEqual(record.account_id, 7)


class ScopeTest(_ApiKeyBase):
    def test_empty_scope_list_means_read_only(self):
        self.assertEqual(normalize_scopes(None), ["read"])
        self.assertEqual(normalize_scopes([]), ["read"])

    def test_scopes_are_sorted_and_deduplicated(self):
        self.assertEqual(normalize_scopes(["trade", "read", "read"]), ["read", "trade"])

    def test_withdraw_scope_does_not_exist(self):
        self.assertNotIn("withdraw", SCOPES)
        with self.assertRaises(HTTPException) as ctx:
            normalize_scopes(["withdraw"])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_parse_scopes_ignores_unknown_and_garbage(self):
        self.assertEqual(parse_scopes('["read", "withdraw"]'), ["read"])
        self.assertEqual(parse_scopes("not json"), [])
        self.assertEqual(parse_scopes(None), [])


class AuthenticateTest(_ApiKeyBase):
    def _authenticate(self, secret: str | None) -> AuthContext | None:
        with _configured_hash_secret():
            return authenticate_api_key(self.db, secret)

    def test_valid_key_returns_owner_context(self):
        record, secret = self._make_key(name="bot", scopes=["read", "trade"])
        context = self._authenticate(secret)
        self.assertIsNotNone(context)
        self.assertEqual(context.user_id, 1)
        self.assertEqual(context.wallet_address, WALLET)
        self.assertEqual(context.scopes, frozenset({"read", "trade"}))
        self.assertEqual(context.key_id, record.id)
        self.assertTrue(context.has_scope("trade"))

    def test_last_used_at_is_recorded_once_and_throttled(self):
        _, secret = self._make_key(name="bot")
        record = self.db.query(ApiKey).one()
        self.assertIsNone(record.last_used_at)

        self._authenticate(secret)
        first = self.db.query(ApiKey).one().last_used_at
        self.assertIsNotNone(first)

        # A second authentication inside the throttle window writes nothing.
        self._authenticate(secret)
        self.assertEqual(self.db.query(ApiKey).one().last_used_at, first)

        # Once the window passes, the stamp moves.
        record.last_used_at = utc_now().replace(year=2000)
        self.db.commit()
        self._authenticate(secret)
        self.assertGreater(self.db.query(ApiKey).one().last_used_at, first)

    def test_revoked_key_is_inert(self):
        record, secret = self._make_key(name="bot")
        revoke_api_key(self.db, 1, record.id)
        self.assertIsNone(self._authenticate(secret))

    def test_expired_key_is_inert(self):
        _, secret = self._make_key(name="bot", expires_in_days=1)
        record = self.db.query(ApiKey).one()
        record.expires_at = utc_now().replace(year=2000)
        self.db.commit()
        self.assertIsNone(self._authenticate(secret))

    def test_naive_expiry_is_treated_as_utc(self):
        _, secret = self._make_key(name="bot", expires_in_days=1)
        record = self.db.query(ApiKey).one()
        record.expires_at = record.expires_at.replace(tzinfo=None)
        self.db.commit()
        self.assertIsNotNone(self._authenticate(secret))

    def test_absent_malformed_and_unknown_keys_all_return_none(self):
        self._make_key(name="bot")
        self.assertIsNone(self._authenticate(None))
        self.assertIsNone(self._authenticate(""))
        self.assertIsNone(self._authenticate("   "))
        self.assertIsNone(self._authenticate("pk_too_short"))
        self.assertIsNone(self._authenticate("pk_completely_unknown_secret_value"))

    def test_two_rows_sharing_a_prefix_resolve_to_the_matching_one(self):
        # Force the pathological case: two distinct secrets that share a
        # public prefix, so the digest comparison (not the first hit)
        # decides which row authenticates.
        shared_head = "AAAAAAAA"
        distinct = iter([f"{shared_head}-one", f"{shared_head}-two"])
        with (
            _configured_hash_secret(),
            patch.object(api_key_service.secrets, "token_urlsafe", lambda _size: next(distinct)),
        ):
            first, first_secret = create_api_key(self.db, user_id=1, name="one")
            second, second_secret = create_api_key(self.db, user_id=1, name="two")
        self.assertNotEqual(first_secret, second_secret)
        self.assertEqual(first.key_prefix, second.key_prefix)

        self.assertEqual(self._authenticate(first_secret).key_id, first.id)
        self.assertEqual(self._authenticate(second_secret).key_id, second.id)

    def test_unconfigured_hash_secret_fails_closed(self):
        _, secret = self._make_key(name="bot")
        with (
            patch.object(
                api_key_service,
                "get_settings",
                return_value=SimpleNamespace(api_key_hash_secret="", jwt_secret_key=""),
            ),
            self.assertRaises(ValueError),
        ):
            authenticate_api_key(self.db, secret)

    def test_key_without_an_owner_is_refused(self):
        record, secret = self._make_key(name="bot")
        record.user_id = 4242
        self.db.commit()
        self.assertIsNone(self._authenticate(secret))


class ListAndRevokeTest(_ApiKeyBase):
    def test_list_is_owner_scoped(self):
        self._make_key(name="mine", user_id=1)
        self._make_key(name="theirs", user_id=2)
        listed = list_api_keys(self.db, 1)
        self.assertEqual([record.name for record in listed], ["mine"])

    def test_revoking_a_foreign_key_returns_none(self):
        record, _ = self._make_key(name="theirs", user_id=2)
        self.assertIsNone(revoke_api_key(self.db, 1, record.id))
        self.assertIsNone(self.db.query(ApiKey).one().revoked_at)

    def test_revoke_is_idempotent_and_final(self):
        record, _ = self._make_key(name="bot")
        revoked = revoke_api_key(self.db, 1, record.id)
        self.assertIsNotNone(revoked.revoked_at)
        again = revoke_api_key(self.db, 1, record.id)
        self.assertIsNotNone(again.revoked_at)


# ── Dependency ──────────────────────────────────────────────


class DummySession:
    """Minimal session for a dependency-level test with no database."""

    def query(self, *args, **kwargs):
        raise AssertionError("no query should run")


class RequireApiKeyTest(unittest.TestCase):
    """The HTTP status each failure mode produces."""

    def setUp(self):
        self.dependency_factory = require_api_key

    def _call(self, header_value, *, scope="read", session=None, authenticate=None):
        dependency = require_api_key(scope)
        return dependency(
            x_api_key=header_value,
            db=session if session is not None else DummySession(),
        )

    def test_missing_header_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_blank_header_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call("   ")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_unknown_key_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call("pk_nope")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_scope_is_403(self):
        context = AuthContext(
            user_id=1,
            account_id=None,
            wallet_address=WALLET,
            key_id=1,
            key_prefix="pk_test",
            scopes=frozenset({"read"}),
        )
        with (
            patch("app.api.deps.authenticate_api_key", return_value=context),
            self.assertRaises(HTTPException) as ctx,
        ):
            self._call("pk_real_secret_value", scope="trade")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_right_scope_passes(self):
        context = AuthContext(
            user_id=1,
            account_id=None,
            wallet_address=WALLET,
            key_id=1,
            key_prefix="pk_test",
            scopes=frozenset({"trade"}),
        )
        with patch("app.api.deps.authenticate_api_key", return_value=context):
            self.assertEqual(self._call("pk_real_secret_value", scope="trade"), context)

    def test_unconfigured_hash_secret_is_503(self):
        with (
            patch("app.api.deps.authenticate_api_key", side_effect=ValueError("no secret")),
            self.assertRaises(HTTPException) as ctx,
        ):
            self._call("pk_real_secret_value")
        self.assertEqual(ctx.exception.status_code, 503)


class RateLimitKeyTest(unittest.TestCase):
    """Internal and external limits must live in separate buckets."""

    def _request(self, api_key: str | None = None):
        from starlette.requests import Request

        headers = [(b"x-api-key", api_key.encode())] if api_key else []
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/external/portfolio",
            "headers": headers,
        }
        return Request(scope)

    def test_keyed_by_key_prefix_when_a_key_is_present(self):
        secret = f"pk_{'a' * 8}{'b' * 20}"
        key = external_api_rate_limit_key(self._request(secret))
        self.assertEqual(key, f"api_key:{secret[:KEY_PREFIX_LENGTH]}")
        self.assertNotIn(secret, key)

    def test_falls_back_to_the_client_ip(self):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/external/markets",
            "headers": [],
            "client": ("203.0.113.7", 5000),
        }
        self.assertEqual(
            external_api_rate_limit_key(Request(scope)),
            "ip:203.0.113.7",
        )


# ── Management routes (internal, session auth) ──────────────


API_KEY_TEST_APP = FastAPI()
API_KEY_TEST_APP.include_router(external_module.api_keys_router)


class ApiKeyManagementRouteTest(_ApiKeyBase):
    """The browser surface: mint, list, revoke — session auth only."""

    def setUp(self):
        super().setUp()
        API_KEY_TEST_APP.dependency_overrides[get_db] = self._get_db_override
        self._session()
        self.client = TestClient(API_KEY_TEST_APP)
        self.addCleanup(self._cleanup_routes)

    def _cleanup_routes(self):
        API_KEY_TEST_APP.dependency_overrides.clear()

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _anonymous(self):
        API_KEY_TEST_APP.dependency_overrides.pop(get_current_user_from_token, None)

    def _session(self, user_id: int = 1):
        API_KEY_TEST_APP.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": user_id,
            "wallet_address": WALLET,
            "is_admin": False,
        }

    def _patch_hash_secret(self):
        patcher = patch.object(
            api_key_service,
            "get_settings",
            return_value=SimpleNamespace(api_key_hash_secret=HASH_SECRET, jwt_secret_key=""),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_requires_a_session(self):
        self._anonymous()
        response = self.client.post("/api/settings/api-keys", json={"name": "bot"})
        self.assertEqual(response.status_code, 401)

    def test_create_shows_the_secret_exactly_once(self):
        self._patch_hash_secret()
        created = self.client.post("/api/settings/api-keys", json={"name": "bot"}).json()
        self.assertEqual(created["name"], "bot")
        self.assertEqual(created["scopes"], ["read"])
        self.assertTrue(created["secret"].startswith("pk_"))

        listed = self.client.get("/api/settings/api-keys").json()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("secret", listed[0])
        self.assertNotIn("key_hash", listed[0])
        self.assertEqual(listed[0]["key_prefix"], created["key_prefix"])

    def test_create_rejects_an_unknown_scope(self):
        self._patch_hash_secret()
        response = self.client.post(
            "/api/settings/api-keys", json={"name": "bot", "scopes": ["withdraw"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.query(ApiKey).count(), 0)

    def test_create_refuses_to_scope_a_key_to_a_foreign_account(self):
        self._patch_hash_secret()
        foreign = User(id=3, wallet_address="0x" + "ef" * 20)
        self.db.add(foreign)
        self.db.commit()
        response = self.client.post(
            "/api/settings/api-keys",
            json={"name": "bot", "account_id": 4242},
        )
        self.assertEqual(response.status_code, 404)

    def test_list_never_leaks_another_users_keys(self):
        self._patch_hash_secret()
        self._make_key(name="mine", user_id=1)
        self._make_key(name="theirs", user_id=2)
        listed = self.client.get("/api/settings/api-keys").json()
        self.assertEqual([entry["name"] for entry in listed], ["mine"])

    def test_revoke_then_refuse_to_revoke_again(self):
        self._patch_hash_secret()
        created = self.client.post("/api/settings/api-keys", json={"name": "bot"}).json()
        revoked = self.client.delete(f"/api/settings/api-keys/{created['id']}").json()
        self.assertTrue(revoked["revoked"])
        self.assertIsNotNone(revoked["revoked_at"])

        missing = self.client.delete("/api/settings/api-keys/9999")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(self.client.get("/api/settings/api-keys").json()[0]["revoked"], True)

    def test_management_routes_do_not_accept_an_api_key(self):
        record, secret = self._make_key(name="bot")
        self._anonymous()
        headers = {"X-API-Key": secret}
        # No session, only the external credential: 401, not 200/201.
        self.assertEqual(
            self.client.post(
                "/api/settings/api-keys", json={"name": "x"}, headers=headers
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/api/settings/api-keys", headers=headers).status_code,
            401,
        )
        self.assertEqual(
            self.client.delete(f"/api/settings/api-keys/{record.id}", headers=headers).status_code,
            401,
        )


if __name__ == "__main__":
    unittest.main()
