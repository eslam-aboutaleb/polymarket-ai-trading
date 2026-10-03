import os
import unittest

from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

os.environ.setdefault("JWT_SECRET_KEY", "a" * 32)

from app.api.routes.auth import (
    ACCESS_TOKEN_COOKIE_NAME,
    REFRESH_TOKEN_COOKIE_NAME,
    _clear_auth_cookies,
    _extract_bearer_token,
    _set_auth_cookies,
    _validate_origin,
)


def _request_with_origin(origin: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/refresh",
        "headers": [(b"origin", origin.encode("utf-8"))],
    }
    return Request(scope)


class AuthCookieHelperTests(unittest.TestCase):
    def test_extract_bearer_token(self):
        self.assertEqual(
            _extract_bearer_token("Bearer abc.def.ghi"),
            "abc.def.ghi",
        )
        self.assertEqual(
            _extract_bearer_token("bearer token-value"),
            "token-value",
        )
        self.assertIsNone(_extract_bearer_token("Basic foo"))
        self.assertIsNone(_extract_bearer_token(None))

    def test_set_auth_cookies_applies_secure_attributes(self):
        response = Response()
        _set_auth_cookies(
            response=response,
            access_token="access-token",
            refresh_token="refresh-token",
            refresh_max_age_seconds=300,
        )
        cookie_headers = response.headers.getlist("set-cookie")
        self.assertEqual(len(cookie_headers), 2)
        self.assertTrue(any(ACCESS_TOKEN_COOKIE_NAME in c for c in cookie_headers))
        self.assertTrue(any(REFRESH_TOKEN_COOKIE_NAME in c for c in cookie_headers))
        self.assertTrue(all("HttpOnly" in c for c in cookie_headers))
        self.assertTrue(all("SameSite=strict" in c for c in cookie_headers))

    def test_clear_auth_cookies_issues_delete_headers(self):
        response = Response()
        _clear_auth_cookies(response)
        cookie_headers = response.headers.getlist("set-cookie")
        self.assertEqual(len(cookie_headers), 2)
        self.assertTrue(any(f"{ACCESS_TOKEN_COOKIE_NAME}=" in c for c in cookie_headers))
        self.assertTrue(any(f"{REFRESH_TOKEN_COOKIE_NAME}=" in c for c in cookie_headers))
        self.assertTrue(all("Max-Age=0" in c for c in cookie_headers))

    def test_origin_validation_rejects_unknown_origin(self):
        request = _request_with_origin("https://evil.example")
        with self.assertRaises(HTTPException) as ctx:
            _validate_origin(request)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_origin_validation_allows_local_dev_origin(self):
        request = _request_with_origin("http://localhost:5173")
        _validate_origin(request)


if __name__ == "__main__":
    unittest.main()
