import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.responses import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.config import get_settings
from app.api.routes.auth import refresh_access_token, logout
from app.models.token import RefreshToken
from app.models.user import User
from app.schemas.auth import LogoutRequest, TokenRefresh
from app.security.refresh_tokens import (
    create_refresh_token_record,
    find_active_refresh_token_record,
    hash_refresh_token,
)


def _request(path: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
    }
    return Request(scope)


class RefreshTokenSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "JWT_SECRET_KEY": "a" * 32,
                "REFRESH_TOKEN_HASH_SECRET": "b" * 32,
                "ENVIRONMENT": "test",
            },
            clear=False,
        )
        self.env_patcher.start()
        get_settings.cache_clear()

        self.engine = create_engine("sqlite:///:memory:")
        User.__table__.create(self.engine)
        RefreshToken.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, class_=Session)
        self.db = self.session_factory()

        self.user = User(wallet_address="0x1234567890abcdef1234567890abcdef12345678")
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.env_patcher.stop()
        get_settings.cache_clear()

    def test_hash_is_deterministic_and_rejects_empty(self):
        h1 = hash_refresh_token("refresh-token")
        h2 = hash_refresh_token("refresh-token")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, hash_refresh_token("different-token"))
        with self.assertRaises(ValueError):
            hash_refresh_token("")

    def test_refresh_rotates_and_revokes_previous_token(self):
        old_token = "old.refresh.token"
        create_refresh_token_record(
            self.db,
            user_id=self.user.id,
            raw_refresh_token=old_token,
            expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        )
        self.db.commit()

        with patch(
            "app.api.routes.auth.verify_token",
            return_value={
                "type": "refresh",
                "sub": self.user.wallet_address,
                "user_id": self.user.id,
                "keep_logged_in": False,
            },
        ):
            response_payload = refresh_access_token(
                http_request=_request("/api/auth/refresh"),
                response=Response(),
                request=TokenRefresh(refresh_token=old_token),
                db=self.db,
            )

        self.assertNotEqual(response_payload.refresh_token, old_token)
        self.assertIsNone(
            find_active_refresh_token_record(self.db, raw_refresh_token=old_token)
        )
        self.assertIsNotNone(
            find_active_refresh_token_record(
                self.db, raw_refresh_token=response_payload.refresh_token
            )
        )
        rows = self.db.query(RefreshToken).filter(RefreshToken.user_id == self.user.id).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for row in rows if row.is_revoked), 1)
        self.assertEqual(sum(1 for row in rows if not row.is_revoked), 1)

    def test_logout_revokes_hashed_refresh_token(self):
        token = "logout.refresh.token"
        create_refresh_token_record(
            self.db,
            user_id=self.user.id,
            raw_refresh_token=token,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.db.commit()

        logout(
            http_request=_request("/api/auth/logout"),
            response=Response(),
            request=LogoutRequest(refresh_token=token),
            db=self.db,
        )

        row = self.db.query(RefreshToken).filter(RefreshToken.user_id == self.user.id).one()
        self.assertTrue(row.is_revoked)


if __name__ == "__main__":
    unittest.main()
