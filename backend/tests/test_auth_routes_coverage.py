"""Coverage tests for app/api/routes/auth.py.

Exercises every route handler and branch of the auth module over
HTTP (TestClient) and via direct handler calls where mock DB
sessions are needed (IntegrityError race paths). External I/O
(JWT minting, signature verification, credential storage, CLOB
client) is patched; the challenge cache and the SQLite session
are real so the login -> verify -> refresh -> logout lifecycle
is exercised end to end.
"""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import app.api.routes.auth as auth_module
from app.api.routes.auth import (
    ACCESS_TOKEN_COOKIE_NAME,
    REFRESH_TOKEN_COOKIE_NAME,
    _login_rate_limit,
    _login_with_key_rate_limit,
    _refresh_rate_limit,
    _verify_rate_limit,
    get_current_user_from_token,
    get_optional_user_from_token,
    login_with_private_key,
    verify_signature,
)
from app.config import get_settings
from app.main import app
from app.models.token import RefreshToken
from app.models.user import User
from app.schemas.auth import (
    PrivateKeyLoginRequest,
    SignatureVerification,
)
from app.security.auth import create_access_token, create_refresh_token
from app.security.credential_store import CredentialStoreError
from app.security.refresh_tokens import (
    create_refresh_token_record,
    find_active_refresh_token_record,
)
from app.utils.cache import challenge_cache
from app.utils.database import get_db

WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20
PRIVATE_KEY = "0x" + "11" * 32
ALLOWED_ORIGIN = "http://localhost:5173"
EVIL_ORIGIN = "https://evil.example"


def _request(path: str = "/api/auth/verify", origin: str | None = None) -> Request:
    headers = []
    if origin:
        headers.append((b"origin", origin.encode("utf-8")))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    return Request(scope)


def _request_with_cookies(path: str, cookies: dict) -> Request:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(b"cookie", cookie_header.encode("utf-8"))],
    }
    return Request(scope)


class AuthRouteTestBase(unittest.TestCase):
    """Shared fixtures: in-memory DB, dependency overrides, TestClient."""

    def setUp(self):
        # StaticPool + check_same_thread=False: TestClient runs sync
        # dependencies in a threadpool, so the in-memory connection must
        # be shareable across threads.
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        User.__table__.create(self.engine)
        RefreshToken.__table__.create(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )
        self.db = self.session_factory()

        app.dependency_overrides[get_db] = self._get_db_override
        for dep in (
            _login_rate_limit,
            _verify_rate_limit,
            _login_with_key_rate_limit,
            _refresh_rate_limit,
        ):
            app.dependency_overrides[dep] = lambda: None

        challenge_cache.clear()
        self.client = TestClient(app)
        self.addCleanup(self._cleanup)

    def _get_db_override(self):
        try:
            yield self.db
        except Exception:
            self.db.rollback()
            raise

    def _cleanup(self):
        app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()
        challenge_cache.clear()

    def _create_user(self, wallet_address: str = WALLET, **kwargs) -> User:
        user = User(wallet_address=wallet_address, **kwargs)
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def _create_refresh_record(
        self, raw_token: str, user_id: int, expires_at: datetime
    ) -> RefreshToken:
        record = create_refresh_token_record(
            self.db, user_id=user_id, raw_refresh_token=raw_token, expires_at=expires_at
        )
        self.db.commit()
        self.db.refresh(record)
        return record

    def _login(self, wallet_address: str = WALLET) -> str:
        resp = self.client.post("/api/auth/login", json={"wallet_address": wallet_address})
        self.assertEqual(resp.status_code, 200)
        return resp.json()["challenge_id"]


class LoginRouteTests(AuthRouteTestBase):
    def test_login_returns_challenge_and_stores_in_cache(self):
        resp = self.client.post("/api/auth/login", json={"wallet_address": WALLET})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("challenge_id", data)
        self.assertIn("challenge", data)
        self.assertIn("timestamp", data)
        self.assertIn("nonce", data)
        self.assertEqual(data["message"], "Please sign this message with your wallet")

        challenge_id = data["challenge_id"]
        stored = challenge_cache.get(f"challenge:{challenge_id}")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["wallet_address"], WALLET)
        self.assertIn("challenge", stored)
        self.assertIn("nonce", stored)
        self.assertEqual(challenge_cache.get(f"wallet_challenge:{WALLET}"), challenge_id)

    def test_login_normalizes_wallet_address_to_lowercase(self):
        resp = self.client.post("/api/auth/login", json={"wallet_address": "0x" + "AB" * 20})

        self.assertEqual(resp.status_code, 200)
        challenge_id = resp.json()["challenge_id"]
        stored = challenge_cache.get(f"challenge:{challenge_id}")
        self.assertEqual(stored["wallet_address"], WALLET)
        self.assertEqual(challenge_cache.get(f"wallet_challenge:{WALLET}"), challenge_id)

    def test_login_retires_previous_challenge_for_wallet(self):
        first_id = self._login()
        second_id = self._login()

        self.assertNotEqual(first_id, second_id)
        self.assertIsNone(challenge_cache.get(f"challenge:{first_id}"))
        self.assertIsNotNone(challenge_cache.get(f"challenge:{second_id}"))
        self.assertEqual(challenge_cache.get(f"wallet_challenge:{WALLET}"), second_id)

    def test_login_rejects_invalid_wallet_address(self):
        resp = self.client.post("/api/auth/login", json={"wallet_address": "not-a-wallet"})
        self.assertEqual(resp.status_code, 422)

    def test_login_rejects_disallowed_origin(self):
        resp = self.client.post(
            "/api/auth/login",
            json={"wallet_address": WALLET},
            headers={"Origin": EVIL_ORIGIN},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["detail"], "Origin not allowed")

    def test_login_allows_configured_origin(self):
        resp = self.client.post(
            "/api/auth/login",
            json={"wallet_address": WALLET},
            headers={"Origin": ALLOWED_ORIGIN},
        )
        self.assertEqual(resp.status_code, 200)

    def test_login_allows_origin_matching_allowed_netloc(self):
        # Same scheme+netloc as an allowed origin but with a path:
        # exercises the urlparse comparison branch of _is_origin_allowed.
        resp = self.client.post(
            "/api/auth/login",
            json={"wallet_address": WALLET},
            headers={"Origin": "http://localhost:5173/callback"},
        )
        self.assertEqual(resp.status_code, 200)


class VerifySignatureTests(AuthRouteTestBase):
    def test_verify_happy_path_creates_user_and_sets_cookies(self):
        challenge_id = self._login()

        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "create_access_token", return_value="access-token-1"),
            patch.object(
                auth_module, "create_refresh_token", return_value="refresh-token-1"
            ) as create_refresh_mock,
        ):
            resp = self.client.post(
                "/api/auth/verify",
                json={
                    "wallet_address": WALLET,
                    "signature": "0xsig",
                    "challenge_id": challenge_id,
                },
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Tokens are delivered via httpOnly cookies only, never the body.
        self.assertIsNone(data["access_token"])
        self.assertIsNone(data["refresh_token"])
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertEqual(data["expires_in"], get_settings().access_token_expire_minutes * 60)

        self.assertEqual(resp.cookies.get(ACCESS_TOKEN_COOKIE_NAME), "access-token-1")
        self.assertEqual(resp.cookies.get(REFRESH_TOKEN_COOKIE_NAME), "refresh-token-1")

        user = self.db.query(User).filter(User.wallet_address == WALLET).first()
        self.assertIsNotNone(user)
        record = find_active_refresh_token_record(self.db, raw_refresh_token="refresh-token-1")
        self.assertIsNotNone(record)
        self.assertEqual(record.user_id, user.id)

        # Challenge is single-use: consumed on verification.
        self.assertIsNone(challenge_cache.get(f"challenge:{challenge_id}"))
        self.assertIsNone(challenge_cache.get(f"wallet_challenge:{WALLET}"))

        create_refresh_mock.assert_called_once()
        self.assertFalse(create_refresh_mock.call_args.kwargs.get("keep_logged_in", False))

    def test_verify_existing_user_updates_last_login(self):
        # SQLite returns naive datetimes even for tz-aware columns.
        user = self._create_user(last_login=datetime(2020, 1, 1))
        challenge_id = self._login()

        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
        ):
            resp = self.client.post(
                "/api/auth/verify",
                json={"wallet_address": WALLET, "signature": "0x", "challenge_id": challenge_id},
            )

        self.assertEqual(resp.status_code, 200)
        self.db.refresh(user)
        self.assertGreater(user.last_login, datetime(2020, 1, 1))

    def test_verify_without_challenge_id_falls_back_to_wallet_mapping(self):
        self._login()  # populates wallet_challenge:{wallet}

        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
        ):
            resp = self.client.post(
                "/api/auth/verify",
                json={"wallet_address": WALLET, "signature": "0x", "challenge_id": None},
            )

        self.assertEqual(resp.status_code, 200)

    def test_verify_without_challenge_id_and_no_mapping_is_rejected(self):
        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
        ):
            resp = self.client.post(
                "/api/auth/verify",
                json={"wallet_address": WALLET, "signature": "0x", "challenge_id": None},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()["detail"], "No pending challenge found. Please initiate login first."
        )

    def test_verify_unknown_challenge_id_is_rejected(self):
        resp = self.client.post(
            "/api/auth/verify",
            json={"wallet_address": WALLET, "signature": "0x", "challenge_id": "unknown-id"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json()["detail"], "No pending challenge found. Please initiate login first."
        )

    def test_verify_challenge_wallet_mismatch_is_rejected(self):
        challenge_cache.set(
            "challenge:mismatch",
            {"challenge": "msg", "wallet_address": OTHER_WALLET, "timestamp": "t", "nonce": "n"},
            ttl_seconds=300,
        )
        resp = self.client.post(
            "/api/auth/verify",
            json={"wallet_address": WALLET, "signature": "0x", "challenge_id": "mismatch"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "Challenge does not match wallet address.")

    def test_verify_invalid_signature_is_rejected(self):
        challenge_id = self._login()
        with patch.object(auth_module, "verify_eth_signature", return_value=False):
            resp = self.client.post(
                "/api/auth/verify",
                json={"wallet_address": WALLET, "signature": "0x", "challenge_id": challenge_id},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid signature")
        # Challenge must survive a failed verification attempt.
        self.assertIsNotNone(challenge_cache.get(f"challenge:{challenge_id}"))

    def test_verify_keep_logged_in_sets_90_day_refresh_cookie(self):
        challenge_id = self._login()
        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
        ):
            resp = self.client.post(
                "/api/auth/verify",
                json={
                    "wallet_address": WALLET,
                    "signature": "0x",
                    "challenge_id": challenge_id,
                    "keep_logged_in": True,
                },
            )
        self.assertEqual(resp.status_code, 200)
        refresh_cookies = [
            c
            for c in resp.headers.get_list("set-cookie")
            if c.startswith(REFRESH_TOKEN_COOKIE_NAME)
        ]
        self.assertTrue(any("Max-Age=7776000" in c for c in refresh_cookies))

    def test_verify_default_refresh_cookie_is_7_days(self):
        challenge_id = self._login()
        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
        ):
            resp = self.client.post(
                "/api/auth/verify",
                json={"wallet_address": WALLET, "signature": "0x", "challenge_id": challenge_id},
            )
        refresh_cookies = [
            c
            for c in resp.headers.get_list("set-cookie")
            if c.startswith(REFRESH_TOKEN_COOKIE_NAME)
        ]
        self.assertTrue(any("Max-Age=604800" in c for c in refresh_cookies))

    def test_verify_rejects_disallowed_origin(self):
        resp = self.client.post(
            "/api/auth/verify",
            json={"wallet_address": WALLET, "signature": "0x", "challenge_id": "x"},
            headers={"Origin": EVIL_ORIGIN},
        )
        self.assertEqual(resp.status_code, 403)

    def test_verify_rejects_invalid_wallet_address(self):
        resp = self.client.post(
            "/api/auth/verify",
            json={"wallet_address": "0xbad", "signature": "0x", "challenge_id": "x"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_verify_concurrent_insert_race_reuses_existing_user(self):
        existing = User(wallet_address=WALLET)
        existing.id = 7
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.side_effect = [None, existing]
        mock_db.commit.side_effect = [
            IntegrityError("INSERT INTO users", {}, Exception("unique violation")),
            None,
        ]
        challenge_cache.set(
            "challenge:race",
            {"challenge": "msg", "wallet_address": WALLET, "timestamp": "t", "nonce": "n"},
            ttl_seconds=300,
        )

        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
            patch.object(auth_module, "create_refresh_token_record"),
        ):
            result = verify_signature(
                request=SignatureVerification(
                    wallet_address=WALLET, signature="0x", challenge_id="race"
                ),
                http_request=_request(),
                response=Response(),
                db=mock_db,
            )

        self.assertEqual(result.wallet_address, WALLET)
        mock_db.rollback.assert_called_once()
        mock_db.refresh.assert_called_once_with(existing)

    def test_verify_concurrent_insert_race_without_user_is_500(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.side_effect = [None, None]
        mock_db.commit.side_effect = [
            IntegrityError("INSERT INTO users", {}, Exception("unique violation")),
        ]
        challenge_cache.set(
            "challenge:race2",
            {"challenge": "msg", "wallet_address": WALLET, "timestamp": "t", "nonce": "n"},
            ttl_seconds=300,
        )

        with (
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            self.assertRaises(HTTPException) as ctx,
        ):
            verify_signature(
                request=SignatureVerification(
                    wallet_address=WALLET, signature="0x", challenge_id="race2"
                ),
                http_request=_request(),
                response=Response(),
                db=mock_db,
            )
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.detail, "Failed to create user. Please try again.")


class LoginWithPrivateKeyTests(AuthRouteTestBase):
    def _clob_client_mock(self):
        creds = MagicMock()
        creds.api_key = "api-key"
        creds.api_secret = "api-secret"
        creds.api_passphrase = "api-passphrase"
        client = MagicMock()
        client.create_or_derive_api_creds.return_value = creds
        return client

    def test_login_with_key_happy_path(self):
        clob_client = self._clob_client_mock()
        with (
            patch.object(
                auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)
            ) as sign_mock,
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "store_wallet_credentials") as store_mock,
            patch.object(auth_module, "create_access_token", return_value="access-token-1"),
            patch.object(auth_module, "create_refresh_token", return_value="refresh-token-1"),
            patch("py_clob_client.client.ClobClient", return_value=clob_client),
        ):
            resp = self.client.post("/api/auth/login-with-key", json={"private_key": PRIVATE_KEY})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIsNone(data["access_token"])
        self.assertIsNone(data["refresh_token"])
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertEqual(resp.cookies.get(ACCESS_TOKEN_COOKIE_NAME), "access-token-1")
        self.assertEqual(resp.cookies.get(REFRESH_TOKEN_COOKIE_NAME), "refresh-token-1")

        # The challenge is signed twice (pending + final) with the private key.
        self.assertEqual(sign_mock.call_count, 2)

        user = self.db.query(User).filter(User.wallet_address == WALLET).first()
        self.assertIsNotNone(user)
        record = find_active_refresh_token_record(self.db, raw_refresh_token="refresh-token-1")
        self.assertIsNotNone(record)

        store_mock.assert_called_once()
        kwargs = store_mock.call_args.kwargs
        self.assertEqual(kwargs["wallet_address"], WALLET)
        self.assertEqual(kwargs["private_key"], PRIVATE_KEY)
        self.assertEqual(
            kwargs["clob_creds"],
            {"api_key": "api-key", "api_secret": "api-secret", "api_passphrase": "api-passphrase"},
        )

    def test_login_with_key_adds_0x_prefix_to_bare_private_key(self):
        clob_client = self._clob_client_mock()
        bare_key = "11" * 32  # no 0x prefix
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "store_wallet_credentials") as store_mock,
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
            patch("py_clob_client.client.ClobClient", return_value=clob_client),
        ):
            resp = self.client.post("/api/auth/login-with-key", json={"private_key": bare_key})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(store_mock.call_args.kwargs["private_key"], "0x" + bare_key)

    def test_login_with_key_clob_creds_failure_is_non_fatal(self):
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "store_wallet_credentials") as store_mock,
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
            patch(
                "py_clob_client.client.ClobClient",
                side_effect=Exception("CLOB network down"),
            ),
        ):
            resp = self.client.post("/api/auth/login-with-key", json={"private_key": PRIVATE_KEY})

        self.assertEqual(resp.status_code, 200)
        # Credential storage still happens, without CLOB creds.
        self.assertIsNone(store_mock.call_args.kwargs["clob_creds"])

    def test_login_with_key_existing_user_updates_last_login(self):
        user = self._create_user(last_login=datetime(2020, 1, 1))
        clob_client = self._clob_client_mock()
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "store_wallet_credentials"),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
            patch("py_clob_client.client.ClobClient", return_value=clob_client),
        ):
            resp = self.client.post("/api/auth/login-with-key", json={"private_key": PRIVATE_KEY})

        self.assertEqual(resp.status_code, 200)
        self.db.refresh(user)
        self.assertGreater(user.last_login, datetime(2020, 1, 1))

    def test_login_with_key_invalid_signature_is_400(self):
        # The HTTPException(400, "Invalid private key") raised
        # inside the try block is re-raised as-is (not masked
        # by the blanket `except Exception`).
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=False),
            self.assertRaises(HTTPException) as ctx,
        ):
            login_with_private_key(
                request=PrivateKeyLoginRequest(private_key=PRIVATE_KEY),
                http_request=_request("/api/auth/login-with-key"),
                response=Response(),
                db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Invalid private key")

    def test_login_with_key_credential_store_error_is_503(self):
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(
                auth_module,
                "store_wallet_credentials",
                side_effect=CredentialStoreError("encryption not configured"),
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            login_with_private_key(
                request=PrivateKeyLoginRequest(private_key=PRIVATE_KEY),
                http_request=_request("/api/auth/login-with-key"),
                response=Response(),
                db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(
            ctx.exception.detail,
            "Credential encryption is not configured correctly. Contact support.",
        )

    def test_login_with_key_value_error_is_400(self):
        with (
            patch.object(auth_module, "sign_message_with_key", side_effect=ValueError("bad key")),
            self.assertRaises(HTTPException) as ctx,
        ):
            login_with_private_key(
                request=PrivateKeyLoginRequest(private_key=PRIVATE_KEY),
                http_request=_request("/api/auth/login-with-key"),
                response=Response(),
                db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Invalid private key format")

    def test_login_with_key_unexpected_error_is_400(self):
        with (
            patch.object(
                auth_module, "generate_challenge_message", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            login_with_private_key(
                request=PrivateKeyLoginRequest(private_key=PRIVATE_KEY),
                http_request=_request("/api/auth/login-with-key"),
                response=Response(),
                db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Login failed")

    def test_login_with_key_origin_error_is_403(self):
        # The HTTPException(403, "Origin not allowed") raised by
        # _validate_origin propagates unchanged (not masked by
        # the blanket `except Exception`).
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            self.assertRaises(HTTPException) as ctx,
        ):
            login_with_private_key(
                request=PrivateKeyLoginRequest(private_key=PRIVATE_KEY),
                http_request=_request("/api/auth/login-with-key", origin=EVIL_ORIGIN),
                response=Response(),
                db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Origin not allowed")

    def test_login_with_key_concurrent_insert_race_reuses_existing_user(self):
        existing = User(wallet_address=WALLET)
        existing.id = 9
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.side_effect = [None, existing]
        mock_db.commit.side_effect = [
            IntegrityError("INSERT INTO users", {}, Exception("unique violation")),
            None,
        ]
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "store_wallet_credentials"),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
            patch.object(auth_module, "create_refresh_token_record"),
        ):
            result = login_with_private_key(
                request=PrivateKeyLoginRequest(private_key=PRIVATE_KEY),
                http_request=_request("/api/auth/login-with-key"),
                response=Response(),
                db=mock_db,
            )

        self.assertEqual(result.wallet_address, WALLET)
        mock_db.rollback.assert_called_once()
        mock_db.refresh.assert_called_once_with(existing)

    def test_login_with_key_concurrent_insert_race_without_user_is_500(self):
        # The HTTPException(500) raised inside the
        # `except IntegrityError` handler propagates unchanged
        # (the blanket `except Exception` no longer masks it).
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.side_effect = [None, None]
        mock_db.commit.side_effect = [
            IntegrityError("INSERT INTO users", {}, Exception("unique violation")),
        ]
        with (
            patch.object(auth_module, "sign_message_with_key", return_value=("0xsig", WALLET)),
            patch.object(auth_module, "verify_eth_signature", return_value=True),
            patch.object(auth_module, "store_wallet_credentials"),
            self.assertRaises(HTTPException) as ctx,
        ):
            login_with_private_key(
                request=PrivateKeyLoginRequest(private_key=PRIVATE_KEY),
                http_request=_request("/api/auth/login-with-key"),
                response=Response(),
                db=mock_db,
            )
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.detail, "Failed to create user. Please try again.")

    def test_login_with_key_rejects_disallowed_origin_over_http(self):
        resp = self.client.post(
            "/api/auth/login-with-key",
            json={"private_key": PRIVATE_KEY},
            headers={"Origin": EVIL_ORIGIN},
        )
        # The 403 from _validate_origin propagates unchanged.
        self.assertEqual(resp.status_code, 403)


class RefreshAccessTokenTests(AuthRouteTestBase):
    def test_refresh_happy_path_rotates_tokens(self):
        user = self._create_user()
        self._create_refresh_record(
            "old-refresh-token", user.id, datetime.now(UTC) + timedelta(days=3)
        )

        with (
            patch.object(
                auth_module,
                "verify_token",
                return_value={
                    "type": "refresh",
                    "sub": WALLET,
                    "user_id": user.id,
                    "keep_logged_in": False,
                },
            ),
            patch.object(
                auth_module, "create_access_token", return_value="new-access-token"
            ) as access_mock,
            patch.object(auth_module, "create_refresh_token", return_value="new-refresh-token"),
        ):
            resp = self.client.post(
                "/api/auth/refresh", json={"refresh_token": "old-refresh-token"}
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["access_token"], "new-access-token")
        self.assertEqual(data["refresh_token"], "new-refresh-token")
        self.assertEqual(data["expires_in"], get_settings().access_token_expire_minutes * 60)

        # Old token revoked, new token persisted.
        self.assertIsNone(
            find_active_refresh_token_record(self.db, raw_refresh_token="old-refresh-token")
        )
        new_record = find_active_refresh_token_record(
            self.db, raw_refresh_token="new-refresh-token"
        )
        self.assertIsNotNone(new_record)
        self.assertEqual(new_record.user_id, user.id)

        # is_admin is preserved from the DB user, not the stale token.
        access_mock.assert_called_once()
        self.assertEqual(access_mock.call_args.args[0]["is_admin"], False)
        self.assertEqual(access_mock.call_args.args[0]["user_id"], user.id)

    def test_refresh_without_token_is_401(self):
        resp = self.client.post("/api/auth/refresh")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Refresh token required")

    def test_refresh_accepts_token_from_cookie(self):
        user = self._create_user()
        self._create_refresh_record(
            "cookie-refresh-token", user.id, datetime.now(UTC) + timedelta(days=3)
        )
        with (
            patch.object(
                auth_module,
                "verify_token",
                return_value={"type": "refresh", "sub": WALLET, "user_id": user.id},
            ),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
        ):
            resp = self.client.post(
                "/api/auth/refresh", cookies={REFRESH_TOKEN_COOKIE_NAME: "cookie-refresh-token"}
            )
        self.assertEqual(resp.status_code, 200)

    def test_refresh_invalid_token_is_401(self):
        with patch.object(
            auth_module,
            "verify_token",
            side_effect=HTTPException(status_code=401, detail="Invalid authentication credentials"),
        ):
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": "garbage-token"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid refresh token")

    def test_refresh_access_token_type_is_rejected(self):
        user = self._create_user()
        self._create_refresh_record("typed-token", user.id, datetime.now(UTC) + timedelta(days=3))
        with patch.object(
            auth_module, "verify_token", return_value={"type": "access", "user_id": user.id}
        ):
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": "typed-token"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid token type - refresh token required")

    def test_refresh_unknown_token_is_401(self):
        user = self._create_user()
        with patch.object(
            auth_module,
            "verify_token",
            return_value={"type": "refresh", "sub": WALLET, "user_id": user.id},
        ):
            resp = self.client.post(
                "/api/auth/refresh", json={"refresh_token": "never-stored-token"}
            )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid or expired refresh token")

    def test_refresh_expired_token_is_401(self):
        user = self._create_user()
        self._create_refresh_record(
            "expired-token", user.id, datetime.now(UTC) - timedelta(hours=1)
        )
        with patch.object(
            auth_module,
            "verify_token",
            return_value={"type": "refresh", "sub": WALLET, "user_id": user.id},
        ):
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": "expired-token"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid or expired refresh token")

    def test_refresh_naive_expiry_datetime_is_handled(self):
        # SQLite returns naive datetimes; the route must treat them as UTC.
        user = self._create_user()
        self._create_refresh_record("naive-token", user.id, datetime.utcnow() + timedelta(days=3))
        with (
            patch.object(
                auth_module,
                "verify_token",
                return_value={"type": "refresh", "sub": WALLET, "user_id": user.id},
            ),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(auth_module, "create_refresh_token", return_value="r"),
        ):
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": "naive-token"})
        self.assertEqual(resp.status_code, 200)

    def test_refresh_missing_user_is_401(self):
        # A valid, unexpired record whose user no longer exists:
        # the route must refuse to mint tokens for a stale subject.
        self._create_refresh_record(
            "ghost-user-token", user_id=9999, expires_at=datetime.now(UTC) + timedelta(days=3)
        )
        with patch.object(
            auth_module,
            "verify_token",
            return_value={"type": "refresh", "sub": WALLET, "user_id": 9999},
        ):
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": "ghost-user-token"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid or expired refresh token")

    def test_refresh_keep_logged_in_payload_mints_90_day_token(self):
        user = self._create_user()
        self._create_refresh_record("keep-token", user.id, datetime.now(UTC) + timedelta(days=3))
        with (
            patch.object(
                auth_module,
                "verify_token",
                return_value={
                    "type": "refresh",
                    "sub": WALLET,
                    "user_id": user.id,
                    "keep_logged_in": True,
                },
            ),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(
                auth_module, "create_refresh_token", return_value="r"
            ) as create_refresh_mock,
        ):
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": "keep-token"})

        self.assertEqual(resp.status_code, 200)
        create_refresh_mock.assert_called_once()
        self.assertTrue(create_refresh_mock.call_args.kwargs["keep_logged_in"])
        refresh_cookies = [
            c
            for c in resp.headers.get_list("set-cookie")
            if c.startswith(REFRESH_TOKEN_COOKIE_NAME)
        ]
        self.assertTrue(any("Max-Age=7776000" in c for c in refresh_cookies))

    def test_refresh_long_expiry_inferred_as_keep_logged_in(self):
        # No keep_logged_in claim, but the record expires far in the future:
        # (expires_at - now).days > refresh_token_expire_days triggers the 90-day path.
        user = self._create_user()
        self._create_refresh_record("long-token", user.id, datetime.now(UTC) + timedelta(days=80))
        with (
            patch.object(
                auth_module,
                "verify_token",
                return_value={"type": "refresh", "sub": WALLET, "user_id": user.id},
            ),
            patch.object(auth_module, "create_access_token", return_value="a"),
            patch.object(
                auth_module, "create_refresh_token", return_value="r"
            ) as create_refresh_mock,
        ):
            resp = self.client.post("/api/auth/refresh", json={"refresh_token": "long-token"})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(create_refresh_mock.call_args.kwargs["keep_logged_in"])

    def test_refresh_rejects_disallowed_origin(self):
        user = self._create_user()
        self._create_refresh_record("origin-token", user.id, datetime.now(UTC) + timedelta(days=3))
        with patch.object(
            auth_module,
            "verify_token",
            return_value={"type": "refresh", "sub": WALLET, "user_id": user.id},
        ):
            resp = self.client.post(
                "/api/auth/refresh",
                json={"refresh_token": "origin-token"},
                headers={"Origin": EVIL_ORIGIN},
            )
        self.assertEqual(resp.status_code, 403)


class LogoutTests(AuthRouteTestBase):
    def test_logout_revokes_token_and_purges_credentials(self):
        user = self._create_user()
        record = self._create_refresh_record(
            "logout-token", user.id, datetime.now(UTC) + timedelta(days=3)
        )

        with patch.object(auth_module, "delete_wallet_credentials") as delete_mock:
            resp = self.client.post("/api/auth/logout", json={"refresh_token": "logout-token"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"message": "Successfully logged out"})
        self.db.refresh(record)
        self.assertTrue(record.is_revoked)
        delete_mock.assert_called_once_with(WALLET)

        # Auth cookies are cleared.
        cookie_headers = resp.headers.get_list("set-cookie")
        self.assertTrue(any(f"{ACCESS_TOKEN_COOKIE_NAME}=" in c for c in cookie_headers))
        self.assertTrue(any(f"{REFRESH_TOKEN_COOKIE_NAME}=" in c for c in cookie_headers))
        self.assertTrue(all("Max-Age=0" in c for c in cookie_headers))

    def test_logout_accepts_token_from_cookie(self):
        user = self._create_user()
        record = self._create_refresh_record(
            "logout-cookie-token", user.id, datetime.now(UTC) + timedelta(days=3)
        )
        with patch.object(auth_module, "delete_wallet_credentials"):
            resp = self.client.post(
                "/api/auth/logout", cookies={REFRESH_TOKEN_COOKIE_NAME: "logout-cookie-token"}
            )
        self.assertEqual(resp.status_code, 200)
        self.db.refresh(record)
        self.assertTrue(record.is_revoked)

    def test_logout_without_token_still_clears_cookies(self):
        with patch.object(auth_module, "delete_wallet_credentials") as delete_mock:
            resp = self.client.post("/api/auth/logout")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"message": "Successfully logged out"})
        delete_mock.assert_not_called()

    def test_logout_unknown_token_is_noop(self):
        with patch.object(auth_module, "delete_wallet_credentials") as delete_mock:
            resp = self.client.post("/api/auth/logout", json={"refresh_token": "never-stored"})
        self.assertEqual(resp.status_code, 200)
        delete_mock.assert_not_called()

    def test_logout_credential_deletion_failure_is_non_fatal(self):
        user = self._create_user()
        record = self._create_refresh_record(
            "logout-fail-token", user.id, datetime.now(UTC) + timedelta(days=3)
        )
        with patch.object(
            auth_module, "delete_wallet_credentials", side_effect=RuntimeError("cache down")
        ):
            resp = self.client.post("/api/auth/logout", json={"refresh_token": "logout-fail-token"})
        self.assertEqual(resp.status_code, 200)
        self.db.refresh(record)
        self.assertTrue(record.is_revoked)

    def test_logout_rejects_disallowed_origin(self):
        resp = self.client.post(
            "/api/auth/logout",
            json={"refresh_token": "x"},
            headers={"Origin": EVIL_ORIGIN},
        )
        self.assertEqual(resp.status_code, 403)


class MeEndpointTests(AuthRouteTestBase):
    def _authenticate(self, user_id):
        app.dependency_overrides[get_current_user_from_token] = lambda: {
            "user_id": user_id,
            "wallet_address": WALLET,
            "is_admin": False,
        }

    def test_me_returns_current_user(self):
        user = self._create_user()
        self._authenticate(user.id)
        resp = self.client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], user.id)
        self.assertEqual(data["wallet_address"], WALLET)
        self.assertIn("created_at", data)
        self.assertIn("last_login", data)
        self.assertFalse(data["is_admin"])

    def test_me_returns_404_for_missing_user(self):
        self._authenticate(9999)
        resp = self.client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "User not found")

    def test_me_requires_authentication(self):
        resp = self.client.get("/api/auth/me")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Missing authentication credentials")


class AuthDependencyTests(unittest.TestCase):
    """Direct tests for the auth dependency functions."""

    def test_get_current_user_from_token_requires_credentials(self):
        with self.assertRaises(HTTPException) as ctx:
            get_current_user_from_token(_request("/api/auth/me"), authorization=None)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Missing authentication credentials")
        self.assertEqual(ctx.exception.headers["WWW-Authenticate"], "Bearer")

    def test_get_current_user_from_token_accepts_bearer_token(self):
        token = create_access_token({"sub": WALLET, "user_id": 5, "is_admin": True})
        result = get_current_user_from_token(
            _request("/api/auth/me"), authorization=f"Bearer {token}"
        )
        self.assertEqual(result, {"user_id": 5, "wallet_address": WALLET, "is_admin": True})

    def test_get_current_user_from_token_accepts_lowercase_bearer(self):
        token = create_access_token({"sub": WALLET, "user_id": 5})
        result = get_current_user_from_token(
            _request("/api/auth/me"), authorization=f"bearer {token}"
        )
        self.assertEqual(result["user_id"], 5)
        self.assertFalse(result["is_admin"])

    def test_get_current_user_from_token_rejects_malformed_authorization(self):
        with self.assertRaises(HTTPException) as ctx:
            get_current_user_from_token(_request("/api/auth/me"), authorization="Bearer")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_get_current_user_from_token_rejects_non_bearer_scheme(self):
        with self.assertRaises(HTTPException) as ctx:
            get_current_user_from_token(
                _request("/api/auth/me"), authorization="Basic dXNlcjpwYXNz"
            )
        self.assertEqual(ctx.exception.status_code, 401)

    def test_get_current_user_from_token_rejects_refresh_token(self):
        token = create_refresh_token({"sub": WALLET, "user_id": 5})
        with self.assertRaises(HTTPException) as ctx:
            get_current_user_from_token(_request("/api/auth/me"), authorization=f"Bearer {token}")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Access token required")

    def test_get_current_user_from_token_rejects_invalid_token(self):
        with self.assertRaises(HTTPException) as ctx:
            get_current_user_from_token(_request("/api/auth/me"), authorization="Bearer not-a-jwt")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_get_current_user_from_token_reads_access_cookie(self):
        token = create_access_token({"sub": WALLET, "user_id": 8})
        request = _request_with_cookies("/api/auth/me", {ACCESS_TOKEN_COOKIE_NAME: token})
        result = get_current_user_from_token(request, authorization=None)
        self.assertEqual(result, {"user_id": 8, "wallet_address": WALLET, "is_admin": False})

    def test_get_optional_user_from_token_returns_none_without_credentials(self):
        self.assertIsNone(
            get_optional_user_from_token(_request("/api/portfolio/balance"), authorization=None)
        )

    def test_get_optional_user_from_token_returns_user(self):
        token = create_access_token({"sub": WALLET, "user_id": 3, "is_admin": False})
        result = get_optional_user_from_token(
            _request("/api/portfolio/balance"), authorization=f"Bearer {token}"
        )
        self.assertEqual(result, {"user_id": 3, "wallet_address": WALLET, "is_admin": False})

    def test_get_optional_user_from_token_rejects_wrong_type(self):
        token = create_refresh_token({"sub": WALLET, "user_id": 3})
        self.assertIsNone(
            get_optional_user_from_token(
                _request("/api/portfolio/balance"), authorization=f"Bearer {token}"
            )
        )

    def test_get_optional_user_from_token_swallows_invalid_token(self):
        self.assertIsNone(
            get_optional_user_from_token(
                _request("/api/portfolio/balance"), authorization="Bearer garbage"
            )
        )


if __name__ == "__main__":
    unittest.main()
